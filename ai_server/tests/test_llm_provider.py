"""Tests for TICK-058: `LLM_PROVIDER` selects the chat's model client at startup.

The config surface predates the dispatch -- `LLM_PROVIDER` has been in `.env.example`
since the first deployment and was read only by `ai_server/app/health.py`, which
reports it. These tests cover the selection itself (each accepted value, an
unrecognised one, absent) and the wiring it drives in `_build_model_turn_service`.

TICK-064 changed what that wiring means, and the tests below now assert the new split.
`LLM_PROVIDER` selects the *front door*, which must be local (D3): under `groq` the chat
degrades outright rather than letting an external model see the patient's turn. Groq is
built separately, from its own settings, because it backs one non-writing tool on every
provider (D13) -- so it is no longer true that "the selected provider decides" for
everything, and the tests say which decides what.

The final test is the outbound guard: the exact bytes `HttpGroqClient` puts on the wire,
asserted whole, so nothing can be added to an outbound request without a test failing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ai_server.app.auth import SessionStore
from ai_server.app.main import _build_model_turn_service
from ai_server.llm.general_knowledge import GeneralKnowledgeService
from ai_server.llm.groq import GROQ_MODEL, GroqSettings, HttpGroqClient
from ai_server.llm.local import HttpLocalModelClient
from ai_server.llm.provider import (
    DEFAULT_LLM_PROVIDER,
    LLM_PROVIDERS,
    LlmProviderError,
    selected_llm_provider,
)
from ai_server.llm.tools import AskGeneralKnowledgeCall
from ai_server.privacy.gate import (
    GENERAL_KNOWLEDGE_SYSTEM_PROMPT,
    OutboundPayload,
    mint_restatement,
)

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
RESTATEMENT = "What is a routine physical examination?"

_LLM_ENV = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_API_KEY",
    "OLLAMA_HOST",
    "GROQ_API_KEY",
    "GROQ_ZDR_VERIFIED_ON",
)


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def run(coroutine):
    return asyncio.run(coroutine)


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a known environment: a developer shell exporting any of these would
    otherwise decide what these tests actually assert."""
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)


def _set_groq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GROQ_ZDR_VERIFIED_ON", "2026-08-20")


# --- AC1: selection ---------------------------------------------------------------


def test_an_absent_llm_provider_still_selects_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset is not a request for a different model: every environment ran Groq before
    this ticket, and must keep running it after."""
    _clear_llm_env(monkeypatch)

    assert selected_llm_provider() == "groq"
    assert DEFAULT_LLM_PROVIDER == "groq"


def test_an_empty_llm_provider_selects_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared-but-blank variable is how `.env.example` ships several settings; it
    means "unset", not "invalid"."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "   ")

    assert selected_llm_provider() == "groq"


def test_every_accepted_value_selects_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)

    for provider in LLM_PROVIDERS:
        monkeypatch.setenv("LLM_PROVIDER", provider)
        assert selected_llm_provider() == provider

    assert LLM_PROVIDERS == ("groq", "ollama")


def test_selection_tolerates_case_and_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`health.py` already lower-cases this variable, so the dispatch must agree with
    it -- otherwise one of the two reports a provider the other did not select."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "  Ollama ")

    assert selected_llm_provider() == "ollama"


def test_an_unrecognised_provider_fails_and_names_the_accepted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gemini` is the live case: the root `.env.example` advertised it and no client
    for it exists. Failing loudly beats starting on Groq while the operator believes
    they configured Gemini."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    with pytest.raises(LlmProviderError) as raised:
        selected_llm_provider()

    message = str(raised.value)
    assert "gemini" in message
    assert "groq" in message
    assert "ollama" in message


# --- AC1: the selection reaches the turn service at startup ------------------------


def _store(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "sessions.sqlite3", b"k" * 32)
    store.initialize()
    return store


def test_an_unrecognised_provider_stops_startup_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_build_model_turn_service` runs inside `lifespan`, so raising here stops the
    boot. The existing configuration-error tolerance must not swallow it -- that would
    be the silent default this criterion forbids."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    client = httpx.AsyncClient()

    with pytest.raises(LlmProviderError):
        _build_model_turn_service(client, client, _store(tmp_path), lambda: NOW)


def test_groq_as_the_front_door_degrades_the_chat_rather_than_serving_a_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D3/FR-34: an external model may not be the thing that receives a typed turn.

    Groq credentials are fully present and the chat is still unavailable, because what
    is refused is the *role*, not the configuration.
    """
    _clear_llm_env(monkeypatch)
    _set_groq_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    client = httpx.AsyncClient()

    service = _build_model_turn_service(client, client, _store(tmp_path), lambda: NOW)

    assert service.client is None
    assert service.services.general_knowledge is None


def test_the_local_client_is_wired_when_llm_provider_is_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    monkeypatch.setenv("OLLAMA_HOST", "http://model.test:11434")
    client = httpx.AsyncClient()

    service = _build_model_turn_service(client, client, _store(tmp_path), lambda: NOW)

    local_client = service.client
    assert isinstance(local_client, HttpLocalModelClient)
    assert local_client._settings.model == "llama3.2"
    assert local_client._settings.endpoint == "http://model.test:11434/v1/chat/completions"


def test_groq_backs_general_knowledge_on_the_local_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TICK-064: Groq is wired off its own settings, not off `LLM_PROVIDER` (D13).

    The two questions came apart. Wiring general knowledge off the provider would leave
    `ask_general_knowledge` permanently unavailable on `ollama` -- the only provider the
    chat can actually run on -- which is the bug this asserts against.
    """
    _clear_llm_env(monkeypatch)
    _set_groq_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    client = httpx.AsyncClient()

    service = _build_model_turn_service(client, client, _store(tmp_path), lambda: NOW)

    assert isinstance(service.client, HttpLocalModelClient)
    assert isinstance(service.services.general_knowledge, GeneralKnowledgeService)


def test_absent_groq_credentials_cost_general_knowledge_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC6, at the wiring level. NFR-20 wants the default demo to need no paid LLM
    service, so an unconfigured Groq must be a supported state rather than a failure --
    and it must not take the local front door down with it."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    client = httpx.AsyncClient()

    service = _build_model_turn_service(client, client, _store(tmp_path), lambda: NOW)

    assert service.services.general_knowledge is None
    assert isinstance(service.client, HttpLocalModelClient)


def test_ollama_without_a_model_degrades_instead_of_failing_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recognised provider missing its own settings keeps the existing behaviour --
    an unavailable chat, not a dead server (NFR-20). Only an unrecognised *name* is
    fatal."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    client = httpx.AsyncClient()

    service = _build_model_turn_service(client, client, _store(tmp_path), lambda: NOW)

    assert service.client is None


# --- AC6: what actually goes on the wire for Groq ----------------------------------


def _payload() -> OutboundPayload:
    """Build an outbound payload the only way this codebase can build one.

    There is deliberately no other way to write this helper: `OutboundPayload` composes
    both messages itself from a minted `Restatement`, so a test cannot set up an
    outbound request out of arbitrary strings any more than production can.
    """
    call = AskGeneralKnowledgeCall.model_validate(
        {"tool": "ask_general_knowledge", "arguments": {"restatement": RESTATEMENT}}
    )
    return OutboundPayload.for_question(mint_restatement(call))


def test_the_whole_groq_request_body_is_the_pinned_model_and_two_composed_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted as one equality against a literal written out here.

    Spelling the body out independently of `HttpGroqClient`'s own helpers is what makes
    this a guard rather than a restatement of the implementation: anything added to an
    outbound request -- a reintroduced `scheduling_context`, a folded-in blob, a new
    field -- changes this dict and fails, instead of quietly reaching a third party.
    """
    _clear_llm_env(monkeypatch)
    _set_groq_env(monkeypatch)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "An answer."}}]})

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = HttpGroqClient(GroqSettings.from_environment(), http_client)
            return await client.complete(_payload())

    assert run(scenario()) == "An answer."
    assert str(captured[0].url) == GROQ_ENDPOINT
    assert json.loads(captured[0].content) == {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": GENERAL_KNOWLEDGE_SYSTEM_PROMPT},
            {"role": "user", "content": RESTATEMENT},
        ],
        "stream": False,
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
