"""Tests for TICK-058's OpenAI-compatible local client (`ai_server/llm/local.py`).

Exercised at the wire level against `httpx.MockTransport`, this suite's existing
stand-in for a live HTTP peer, because that is the only level at which the thing this
ticket cares about is observable: that base URL, model id and API key are configuration
rather than constants.

Driven through `tool_call()` since TICK-064. The `complete()`/`stream()` pair these
tests used to drive existed to satisfy the old `GroqClient` Protocol for Groq scheduling
planning; D13 moved planning to this model's own tool call and both methods were deleted
with the Protocol. The tests that covered Groq's `_strict_schema()` quirk staying behind
Groq's adapter, and the scheduling context reaching the model in message content, went
with the outbound scheduling payload they were about.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_server.llm.local import (
    DEFAULT_LOCAL_BASE_URL,
    HttpLocalModelClient,
    LocalModelConfigurationError,
    LocalModelSettings,
    LocalModelUnavailableError,
)
from ai_server.llm.provider import LlmUnavailableError
from ai_server.llm.tools import envelope_json_schema

USER_MESSAGE = "Can you cancel my upcoming appointment?"
ENDPOINT = "http://model.test:11434/v1/chat/completions"
MESSAGES = [
    {"role": "system", "content": "You route one turn."},
    {"role": "user", "content": USER_MESSAGE},
]


def run(coroutine):
    return asyncio.run(coroutine)


def settings(**overrides: object) -> LocalModelSettings:
    values: dict[str, object] = {"model": "llama3.2", "base_url": "http://model.test:11434"}
    values.update(overrides)
    return LocalModelSettings(**values)  # type: ignore[arg-type]


def call_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"tool":"reply","arguments":{"message":"ok"}}'}}]
        },
    )


def capture_call(
    local_settings: LocalModelSettings, response: httpx.Response | None = None
) -> list[httpx.Request]:
    """Run one `tool_call()` against a stub peer and return the requests it received."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response if response is not None else call_response()

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpLocalModelClient(local_settings, http_client).tool_call(
                MESSAGES, schema=envelope_json_schema()
            )

    run(scenario())
    return captured


# --- AC2: base URL, model id and API key are configuration -------------------------


def test_the_request_goes_to_the_configured_base_url_and_model() -> None:
    captured = capture_call(settings())

    body = json.loads(captured[0].content)
    assert str(captured[0].url) == ENDPOINT
    assert body["model"] == "llama3.2"


def test_a_different_base_url_and_model_change_the_request() -> None:
    """The pair that makes this an adapter rather than an Ollama-shaped constant: the
    same class must serve any OpenAI-compatible server (vLLM is TICK-067)."""
    captured = capture_call(settings(base_url="http://gpu-box.internal:8000/", model="qwen2.5"))

    body = json.loads(captured[0].content)
    assert str(captured[0].url) == "http://gpu-box.internal:8000/v1/chat/completions"
    assert body["model"] == "qwen2.5"


def test_no_authorization_header_is_sent_when_no_api_key_is_configured() -> None:
    """Ollama needs no key, and inventing an empty bearer token would be a header the
    operator never asked to send."""
    captured = capture_call(settings())

    assert "authorization" not in captured[0].headers


def test_the_configured_api_key_is_sent_as_a_bearer_token() -> None:
    captured = capture_call(settings(api_key="local-secret"))

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


# --- AC5: an unreachable provider is unavailable at the point of use ---------------


def test_an_unreachable_server_raises_unavailable_rather_than_a_transport_error() -> None:
    """Nothing listening on the configured port is the ordinary case while the model
    server is not running (TICK-059 has not landed)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpLocalModelClient(settings(), http_client).tool_call(
                MESSAGES, schema=envelope_json_schema()
            )

    with pytest.raises(LocalModelUnavailableError):
        run(scenario())


def test_an_error_status_is_unavailable() -> None:
    with pytest.raises(LocalModelUnavailableError):
        capture_call(settings(), httpx.Response(503, json={"error": "model is loading"}))


def test_a_malformed_response_is_unavailable() -> None:
    with pytest.raises(LocalModelUnavailableError):
        capture_call(settings(), httpx.Response(200, json={"choices": []}))


def test_unavailability_is_the_provider_agnostic_error_the_turn_degrades_on() -> None:
    """The point of use, expressed as the type rather than re-simulated.

    `ModelTurnService.stream_reply` catches `LlmUnavailableError`, not this module's
    subclass, and answers with the honest unavailable message (D12; asserted end to end
    in `test_model_turn.py`). That catch only holds while this relationship does, and
    breaking it would surface as an exception escaping the request handler.
    """
    assert issubclass(LocalModelUnavailableError, LlmUnavailableError)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
