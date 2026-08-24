"""Tests for the Groq transport after TICK-064 narrowed it to general knowledge.

This file used to test `GroqWorkflow`: plan validation, the authoritative tool, the
TICK-041 no-second-call guard, and the privacy rejection. D13 deleted the planner --
Groq no longer decides anything, and no authoritative tool runs behind it -- so what is
left to test here is a transport. The privacy rejection it also covered has not been
dropped: it moved to `test_general_knowledge.py`, alongside the dispatcher that now
performs it.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from ai_server.llm.groq import (
    GROQ_MODEL,
    GroqConfigurationError,
    GroqSettings,
    GroqUnavailableError,
    HttpGroqClient,
)
from ai_server.llm.tools import AskGeneralKnowledgeCall
from ai_server.privacy.gate import (
    GENERAL_KNOWLEDGE_SYSTEM_PROMPT,
    OutboundPayload,
    mint_restatement,
)

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
QUESTION = "What is a routine physical examination?"


def run(coroutine):
    return asyncio.run(coroutine)


def settings() -> GroqSettings:
    return GroqSettings(api_key="test-key", zdr_verified_on=date(2026, 1, 1))


def payload(restatement: str = QUESTION) -> OutboundPayload:
    """Build an outbound payload the only way this codebase can build one."""
    call = AskGeneralKnowledgeCall.model_validate(
        {"tool": "ask_general_knowledge", "arguments": {"restatement": restatement}}
    )
    return OutboundPayload.for_question(mint_restatement(call))


def answering(body: object, status: int = 200) -> tuple[HttpGroqClient, list[httpx.Request]]:
    """Return a client whose peer answers `body`, plus the list it records requests in."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(handler)
    return HttpGroqClient(settings(), httpx.AsyncClient(transport=transport)), seen


def answer_body(content: str = "A routine check-up.") -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}]}


# --- Settings ----------------------------------------------------------------------


def test_ticket_010_requires_dated_zdr_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "key")
    monkeypatch.delenv("GROQ_ZDR_VERIFIED_ON", raising=False)

    with pytest.raises(GroqConfigurationError):
        GroqSettings.from_environment()


def test_an_absent_api_key_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_ZDR_VERIFIED_ON", "2026-01-01")

    with pytest.raises(GroqConfigurationError):
        GroqSettings.from_environment()


# --- The request that actually goes on the wire ------------------------------------


def test_the_request_body_is_the_pinned_model_and_exactly_the_composed_messages() -> None:
    """The whole body, asserted as a whole.

    Written as an equality rather than a set of `in` checks so that anything *extra*
    appearing in an outbound body -- a new field, a folded-in context blob, a
    reintroduced `scheduling_context` -- fails here rather than going quietly out to a
    third party.
    """
    client, seen = answering(answer_body())

    run(client.complete(payload()))

    assert len(seen) == 1
    assert seen[0].url == ENDPOINT
    body = _json(seen[0])
    assert body == {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": GENERAL_KNOWLEDGE_SYSTEM_PROMPT},
            {"role": "user", "content": QUESTION},
        ],
        "stream": False,
    }


def test_the_wire_body_no_longer_carries_any_scheduling_context() -> None:
    """D13: scheduling planning is local, so nothing outbound describes scheduling.

    TICK-039 folded `scheduling_context`/`scheduling_rules` into the system message text
    because Groq 400s on unrecognised top-level keys. Both the keys and the fold are
    gone; this asserts the *text* is clean too, since a fold would not show up as a
    top-level key.
    """
    client, seen = answering(answer_body())

    run(client.complete(payload()))

    serialised = seen[0].content.decode()
    for vestige in ("scheduling_context", "scheduling_rules", "open_slots", "slot_token"):
        assert vestige not in serialised


def test_the_api_key_is_sent_as_a_bearer_token_and_nowhere_else() -> None:
    client, seen = answering(answer_body())

    run(client.complete(payload()))

    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert "test-key" not in seen[0].content.decode()


def test_the_models_answer_is_returned_verbatim() -> None:
    client, _ = answering(answer_body("Once a year, usually."))

    assert run(client.complete(payload())) == "Once a year, usually."


# --- Unavailability ----------------------------------------------------------------


def test_an_error_status_is_unavailable() -> None:
    client, _ = answering(answer_body(), status=500)

    with pytest.raises(GroqUnavailableError):
        run(client.complete(payload()))


def test_a_malformed_response_is_unavailable() -> None:
    client, _ = answering({"choices": []})

    with pytest.raises(GroqUnavailableError):
        run(client.complete(payload()))


def test_an_unreachable_endpoint_is_unavailable_rather_than_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening", request=request)

    client = HttpGroqClient(settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(GroqUnavailableError):
        run(client.complete(payload()))


def test_the_groq_client_cannot_be_a_turn_client() -> None:
    """TICK-063's guard, restated now that Groq is wired into a turn's services.

    `ModelTurnService` needs `tool_call()`; `HttpGroqClient` has never had one and must
    not grow one. Groq backs a tool, and cannot become the front door that sees the
    patient's typed turn (D3, FR-34).
    """
    assert not hasattr(HttpGroqClient, "tool_call")


def _json(request: httpx.Request) -> object:
    import json

    return json.loads(request.content.decode())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
