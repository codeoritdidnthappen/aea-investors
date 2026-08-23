"""Tests for TICK-058's OpenAI-compatible local client (`ai_server/llm/local.py`).

Exercised at the wire level against `httpx.MockTransport`, this suite's existing
stand-in for a live HTTP peer, because that is the only level at which the two things
this ticket cares about are observable: that base URL, model id and API key are
configuration rather than constants, and that Groq's `_strict_schema()` quirk stays
behind Groq's adapter (LOCAL_LLM_SPEC Risks: "provider-specific; the adapters must not
assume it generalises").
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_server.app.chat import SYSTEM_PROMPT
from ai_server.llm.groq import (
    PLANNING_FAILED_RESPONSE,
    AuthoritativeToolResult,
    GroqWorkflow,
    PlanningOutput,
)
from ai_server.llm.local import (
    DEFAULT_LOCAL_BASE_URL,
    HttpLocalModelClient,
    LocalModelConfigurationError,
    LocalModelSettings,
    LocalModelUnavailableError,
)
from ai_server.privacy.gate import (
    AnonymousAppointment,
    OutboundMessage,
    OutboundPayload,
    PrivacyGate,
    ResponseFormat,
    SchedulingContext,
    SchedulingRules,
)

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
APPOINTMENT_TOKEN = "appt_local0000000000000000000001"
USER_MESSAGE = "Can you cancel my upcoming appointment?"
ENDPOINT = "http://model.test:11434/v1/chat/completions"


def run(coroutine):
    return asyncio.run(coroutine)


def settings(**overrides: object) -> LocalModelSettings:
    values: dict[str, object] = {"model": "llama3.2", "base_url": "http://model.test:11434"}
    values.update(overrides)
    return LocalModelSettings(**values)  # type: ignore[arg-type]


def payload() -> OutboundPayload:
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


def plan_response() -> httpx.Response:
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


def capture_complete(
    local_settings: LocalModelSettings, response: httpx.Response | None = None
) -> list[httpx.Request]:
    """Run one `complete()` against a stub peer and return the requests it received."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response if response is not None else plan_response()

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpLocalModelClient(local_settings, http_client).complete(payload())

    run(scenario())
    return captured


# --- AC2: base URL, model id and API key are configuration -------------------------


def test_the_request_goes_to_the_configured_base_url_and_model() -> None:
    captured = capture_complete(settings())

    body = json.loads(captured[0].content)
    assert str(captured[0].url) == ENDPOINT
    assert body["model"] == "llama3.2"


def test_a_different_base_url_and_model_change_the_request() -> None:
    """The pair that makes this an adapter rather than an Ollama-shaped constant: the
    same class must serve any OpenAI-compatible server (vLLM is TICK-067)."""
    captured = capture_complete(settings(base_url="http://gpu-box.internal:8000/", model="qwen2.5"))

    body = json.loads(captured[0].content)
    assert str(captured[0].url) == "http://gpu-box.internal:8000/v1/chat/completions"
    assert body["model"] == "qwen2.5"


def test_no_authorization_header_is_sent_when_no_api_key_is_configured() -> None:
    """Ollama needs no key, and inventing an empty bearer token would be a header the
    operator never asked to send."""
    captured = capture_complete(settings())

    assert "authorization" not in captured[0].headers


def test_the_configured_api_key_is_sent_as_a_bearer_token() -> None:
    captured = capture_complete(settings(api_key="local-secret"))

    assert captured[0].headers["Authorization"] == "Bearer local-secret"


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    monkeypatch.setenv("OLLAMA_HOST", "http://model.test:11434")
    monkeypatch.setenv("LLM_API_KEY", "local-secret")

    parsed = LocalModelSettings.from_environment()

    assert parsed == LocalModelSettings(
        model="llama3.2", base_url="http://model.test:11434", api_key="local-secret"
    )
    assert parsed.endpoint == ENDPOINT


def test_the_base_url_falls_back_to_ollamas_default_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "llama3.2")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    parsed = LocalModelSettings.from_environment()

    assert parsed.base_url == DEFAULT_LOCAL_BASE_URL
    assert parsed.api_key is None


def test_an_absent_model_id_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(LocalModelConfigurationError) as raised:
        LocalModelSettings.from_environment()

    assert "LLM_MODEL" in str(raised.value)


# --- AC3: Groq's vendor quirk stays behind Groq's adapter --------------------------


def test_the_planning_request_does_not_inherit_groqs_strict_schema_rewrite() -> None:
    """`_strict_schema()` names every property in `required` and sets `strict: true`
    because Groq 400s otherwise. That was discovered live against Groq; asserting its
    absence here is what stops it being copied into a backend that never asked for
    it."""
    captured = capture_complete(settings())

    json_schema = json.loads(captured[0].content)["response_format"]["json_schema"]
    assert "strict" not in json_schema
    assert json_schema["name"] == "scheduling_plan"
    assert json_schema["schema"] == PlanningOutput.model_json_schema()
    assert json_schema["schema"]["required"] == ["intent"]


def test_the_scheduling_context_reaches_the_local_model_in_message_content() -> None:
    """The one channel a chat-completions endpoint actually feeds to the model, for
    the local server exactly as for Groq (TICK-039)."""
    captured = capture_complete(settings())

    messages = json.loads(captured[0].content)["messages"]
    system_message, user_message = messages
    assert system_message["content"].startswith(SYSTEM_PROMPT)
    assert APPOINTMENT_TOKEN in system_message["content"]
    assert "cancellation_enabled" in system_message["content"]
    assert user_message == {"role": "user", "content": USER_MESSAGE}
    assert set(json.loads(captured[0].content)) == {
        "model",
        "messages",
        "stream",
        "response_format",
    }


def test_complete_returns_the_models_planning_content() -> None:
    """The parsed plan is what `GroqWorkflow` validates, so the adapter must return the
    message content verbatim."""

    async def handler(_: httpx.Request) -> httpx.Response:
        return plan_response()

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpLocalModelClient(settings(), http_client).complete(payload())

    plan = PlanningOutput.model_validate_json(run(scenario()))

    assert plan.intent == "cancel"
    assert plan.appointment_token == APPOINTMENT_TOKEN


# --- AC4: streaming ----------------------------------------------------------------


def test_streaming_yields_each_content_chunk_in_order() -> None:
    captured: list[httpx.Request] = []
    events = (
        b'data: {"choices":[{"delta":{"content":"Your appointment "}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"is cancelled."}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events)

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = HttpLocalModelClient(settings(), http_client)
            return [chunk async for chunk in client.stream(payload())]

    chunks = run(scenario())

    assert chunks == ["Your appointment ", "is cancelled."]
    body = json.loads(captured[0].content)
    assert body["stream"] is True
    # A streamed final response is prose, not a plan: no schema is requested, matching
    # the Groq path.
    assert set(body) == {"model", "messages", "stream"}


def test_stream_is_not_a_coroutine_function() -> None:
    """`GroqClient.stream()` returns an async iterator rather than awaiting one, and a
    client that made it `async def` would break every `async for` call site."""
    assert not asyncio.iscoroutinefunction(HttpLocalModelClient.stream)


# --- AC5: an unreachable provider is unavailable at the point of use ---------------


def test_an_unreachable_server_raises_unavailable_rather_than_a_transport_error() -> None:
    """Nothing listening on the configured port is the ordinary case while the model
    server is not running (TICK-059 has not landed)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpLocalModelClient(settings(), http_client).complete(payload())

    with pytest.raises(LocalModelUnavailableError):
        run(scenario())


def test_an_error_status_is_unavailable() -> None:
    with pytest.raises(LocalModelUnavailableError):
        capture_complete(settings(), httpx.Response(503, json={"error": "model is loading"}))


def test_a_malformed_response_is_unavailable() -> None:
    with pytest.raises(LocalModelUnavailableError):
        capture_complete(settings(), httpx.Response(200, json={"choices": []}))


def test_an_unreachable_stream_raises_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = HttpLocalModelClient(settings(), http_client)
            return [chunk async for chunk in client.stream(payload())]

    with pytest.raises(LocalModelUnavailableError):
        run(scenario())


@dataclass
class RefusingTool:
    async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
        raise AssertionError("no tool may run when planning never succeeded")


def test_an_unreachable_local_model_degrades_the_turn_instead_of_crashing_it() -> None:
    """The point of use: `GroqWorkflow` must catch the local client's unavailability
    the same way it catches Groq's, so the patient gets the honest planning-failed
    reply instead of the request handler raising."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            workflow = GroqWorkflow(
                PrivacyGate.create(), HttpLocalModelClient(settings(), http_client)
            )
            return [chunk async for chunk in workflow.respond(payload(), RefusingTool())]

    assert run(scenario()) == [PLANNING_FAILED_RESPONSE]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
