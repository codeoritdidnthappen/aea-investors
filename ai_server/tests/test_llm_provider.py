"""Tests for TICK-058: `LLM_PROVIDER` selects the chat's model client at startup.

The config surface predates the dispatch -- `LLM_PROVIDER` has been in `.env.example`
since the first deployment and was read only by `ai_server/app/health.py`, which
reports it. These tests cover the selection itself (each accepted value, an
unrecognised one, absent) and the wiring it drives in `_build_chat_service`.

The last test is the seam's own guard: the exact bytes `HttpGroqClient` puts on the
wire must be identical to what it sent before a second provider existed, so adding the
adapter boundary cannot silently alter what an existing provider receives.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_server.app.chat import SYSTEM_PROMPT
from ai_server.app.main import _build_chat_service
from ai_server.llm.groq import GROQ_MODEL, GroqSettings, HttpGroqClient, PlanningOutput
from ai_server.llm.local import HttpLocalModelClient
from ai_server.llm.provider import (
    DEFAULT_LLM_PROVIDER,
    LLM_PROVIDERS,
    LlmProviderError,
    selected_llm_provider,
)
from ai_server.privacy.gate import (
    AnonymousAppointment,
    OutboundMessage,
    OutboundPayload,
    ResponseFormat,
    SchedulingContext,
    SchedulingRules,
)

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
APPOINTMENT_TOKEN = "appt_seam00000000000000000000001"
USER_MESSAGE = "Can you cancel my upcoming appointment?"

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


# --- AC1: the selection reaches the chat service at startup ------------------------


def test_an_unrecognised_provider_stops_startup_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_chat_service` runs inside `lifespan`, so raising here stops the boot.
    The existing `except GroqConfigurationError` tolerance must not swallow it -- that
    would be the silent default this criterion forbids."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    client = httpx.AsyncClient()

    with pytest.raises(LlmProviderError):
        _build_chat_service(client, client, lambda: NOW)


def test_groq_is_wired_when_llm_provider_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    _set_groq_env(monkeypatch)
    client = httpx.AsyncClient()

    service = _build_chat_service(client, client, lambda: NOW)

    assert isinstance(service.workflow._client, HttpGroqClient)  # type: ignore[union-attr]


def test_groq_is_wired_when_llm_provider_is_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    _set_groq_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    client = httpx.AsyncClient()

    service = _build_chat_service(client, client, lambda: NOW)

    assert isinstance(service.workflow._client, HttpGroqClient)  # type: ignore[union-attr]


def test_the_local_client_is_wired_when_llm_provider_is_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Groq credentials are present *and ignored*: the selected provider decides, not
    whichever provider happens to be configured."""
    _clear_llm_env(monkeypatch)
    _set_groq_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    monkeypatch.setenv("OLLAMA_HOST", "http://model.test:11434")
    client = httpx.AsyncClient()

    service = _build_chat_service(client, client, lambda: NOW)

    local_client = service.workflow._client  # type: ignore[union-attr]
    assert isinstance(local_client, HttpLocalModelClient)
    assert local_client._settings.model == "llama3.2"
    assert local_client._settings.endpoint == "http://model.test:11434/v1/chat/completions"


def test_ollama_without_a_model_degrades_instead_of_failing_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognised provider missing its own settings keeps the existing behaviour --
    an unavailable chat, not a dead server (NFR-20). Only an unrecognised *name* is
    fatal."""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    client = httpx.AsyncClient()

    service = _build_chat_service(client, client, lambda: NOW)

    assert service.workflow is None


# --- AC6: the seam changes nothing on the wire for Groq ----------------------------


def _payload() -> OutboundPayload:
    return OutboundPayload(
        model="openai/gpt-oss-120b",
        messages=[
            OutboundMessage(role="system", content=SYSTEM_PROMPT),
            OutboundMessage(role="user", content=USER_MESSAGE),
        ],
        scheduling_context=SchedulingContext(
            current_datetime=NOW,
            timezone="America/Chicago",
            office_hours=[],
            closures=[],
            open_slots=[],
            current_appointments=[
                AnonymousAppointment(
                    appointment_token=APPOINTMENT_TOKEN,
                    starts_at=NOW + timedelta(hours=19),
                    ends_at=NOW + timedelta(hours=19, minutes=30),
                )
            ],
        ),
        scheduling_rules=SchedulingRules(
            minimum_booking_notice_minutes=1440,
            booking_enabled=False,
            rescheduling_enabled=False,
            cancellation_enabled=True,
        ),
        response_format=ResponseFormat(type="json_schema", schema_version="1"),
    )


def _expected_groq_body(*, stream: bool) -> dict[str, object]:
    """The Groq request body spelled out independently of `HttpGroqClient`'s own
    helpers, including the vendor quirk: `strict: true` plus every property named in
    `required`, which is exactly what `_strict_schema()` exists to produce and what the
    local adapter must not inherit."""
    payload = _payload()
    system_content = (
        f"{SYSTEM_PROMPT}\n\n"
        f"scheduling_context: {payload.scheduling_context.model_dump_json()}\n\n"
        f"scheduling_rules: {payload.scheduling_rules.model_dump_json()}"
    )
    body: dict[str, object] = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": USER_MESSAGE},
        ],
        "stream": stream,
    }
    if not stream:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "scheduling_plan",
                "strict": True,
                "schema": {
                    **PlanningOutput.model_json_schema(),
                    "required": ["intent", "slot_token", "appointment_token"],
                },
            },
        }
    return body


def test_the_seam_leaves_the_groq_planning_request_byte_for_byte_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compared as raw bytes, not as a parsed dict: encoded through the same `httpx`
    path the client uses, so neither an added key nor a reordered one can slip past."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "cancel",
                                    "slot_token": None,
                                    "appointment_token": APPOINTMENT_TOKEN,
                                }
                            )
                        }
                    }
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            settings = GroqSettings(api_key="test-groq-key", zdr_verified_on=NOW.date())
            await HttpGroqClient(settings, http_client).complete(_payload())

    for provider in (None, "groq"):
        _clear_llm_env(monkeypatch)
        if provider is not None:
            monkeypatch.setenv("LLM_PROVIDER", provider)
        captured.clear()
        run(scenario())

        request = captured[0]
        expected = httpx.Request("POST", GROQ_ENDPOINT, json=_expected_groq_body(stream=False))
        assert request.content == expected.content
        assert str(request.url) == GROQ_ENDPOINT
        assert request.headers["Authorization"] == "Bearer test-groq-key"


def test_the_seam_leaves_the_groq_streaming_request_byte_for_byte_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"Cancelled."}}]}\n\ndata: [DONE]\n\n',
        )

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            settings = GroqSettings(api_key="test-groq-key", zdr_verified_on=NOW.date())
            client = HttpGroqClient(settings, http_client)
            return [chunk async for chunk in client.stream(_payload())]

    chunks = run(scenario())

    assert chunks == ["Cancelled."]
    expected = httpx.Request("POST", GROQ_ENDPOINT, json=_expected_groq_body(stream=True))
    assert captured[0].content == expected.content


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
