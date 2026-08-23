"""An OpenAI-compatible local model transport behind the same Protocol as Groq's.

LOCAL_LLM_SPEC D7: "Ollama locally, vLLM deployed, selected by config from day one".
Ollama serves the ordinary chat-completions shape at `/v1/chat/completions`, so the
whole adapter is the request/response shaping plus its own settings -- `GroqClient` is
a two-method Protocol and this class implements exactly those two methods.

What is deliberately *not* shared with `HttpGroqClient` is `_strict_schema()`. That
helper exists because Groq's strict structured-output mode rejects a schema whose
`required` omits any property (confirmed live, see its docstring); LOCAL_LLM_SPEC's
Risks section records that as a vendor quirk the adapters "must not assume generalises".
This client therefore sends the plain Pydantic schema, with neither the rewritten
`required` list nor `strict: true`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from ai_server.llm.groq import PlanningOutput
from ai_server.llm.provider import LlmUnavailableError
from ai_server.privacy.gate import OutboundPayload

# Ollama's default listen address, and the value `.env.example` already documents for
# `OLLAMA_HOST`. Only the fallback lives here; the address itself is configuration.
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434"


class LocalModelConfigurationError(Exception):
    """Raised when a required local model deployment control is absent."""


class LocalModelUnavailableError(LlmUnavailableError):
    """Raised when the local model server cannot provide a safe response."""


@dataclass(frozen=True)
class LocalModelSettings:
    """Where the local OpenAI-compatible server is and which model to ask for."""

    model: str
    base_url: str = DEFAULT_LOCAL_BASE_URL
    api_key: str | None = None

    @property
    def endpoint(self) -> str:
        """Return the chat-completions URL of the configured base URL."""
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"

    @classmethod
    def from_environment(cls) -> LocalModelSettings:
        """Parse the local model settings once during application startup.

        `LLM_MODEL` has no defensible default -- a model id this server invented is a
        model the operator's server will 404 on -- so it is required, and its absence
        degrades the chat exactly as an absent `GROQ_API_KEY` does. An API key is
        optional: Ollama needs none, while other OpenAI-compatible servers do.
        """
        model = os.environ.get("LLM_MODEL")
        if not model:
            raise LocalModelConfigurationError("LLM_MODEL is required when LLM_PROVIDER=ollama")
        return cls(
            model=model,
            base_url=os.environ.get("OLLAMA_HOST") or DEFAULT_LOCAL_BASE_URL,
            api_key=os.environ.get("LLM_API_KEY") or None,
        )


class HttpLocalModelClient:
    """OpenAI-compatible local transport; implements the `GroqClient` Protocol.

    Every failure to reach or parse the local server raises
    `LocalModelUnavailableError`, which `GroqWorkflow.respond()` catches via
    `LlmUnavailableError` -- so pointing `LLM_PROVIDER` at a server that is not running
    surfaces as the chat's honest "could not handle that request" reply at the point of
    use, not as an exception escaping the request handler.
    """

    def __init__(self, settings: LocalModelSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def complete(self, payload: OutboundPayload) -> str:
        """Request a JSON plan from the configured local model."""
        try:
            response = await self._client.post(
                self._settings.endpoint,
                headers=self._headers(),
                json=self._request_body(payload, stream=False, structured=True),
            )
        except httpx.HTTPError as exc:
            raise LocalModelUnavailableError("the local model is unavailable") from exc
        if response.status_code != 200:
            raise LocalModelUnavailableError("the local model is unavailable")
        try:
            return self._content(response.json())
        except ValueError as exc:
            raise LocalModelUnavailableError(
                "the local model returned an invalid planning response"
            ) from exc

    async def _stream(self, payload: OutboundPayload) -> AsyncIterator[str]:
        """Yield SSE content chunks without buffering the final response."""
        try:
            async with self._client.stream(
                "POST",
                self._settings.endpoint,
                headers=self._headers(),
                json=self._request_body(payload, stream=True, structured=False),
            ) as response:
                if response.status_code != 200:
                    raise LocalModelUnavailableError("the local model is unavailable")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ")
                    if data == "[DONE]":
                        return
                    try:
                        chunk = self._stream_content(json.loads(data))
                    except (TypeError, ValueError, KeyError) as exc:
                        raise LocalModelUnavailableError(
                            "the local model returned an invalid streamed response"
                        ) from exc
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise LocalModelUnavailableError("the local model is unavailable") from exc

    def stream(self, payload: OutboundPayload) -> AsyncIterator[str]:
        """Return the asynchronous final-response stream.

        Not `async def`, matching `GroqClient.stream()`: callers `async for` over the
        return value directly rather than awaiting it first.
        """
        return self._stream(payload)

    def _headers(self) -> dict[str, str]:
        """Return the request headers; the bearer token only when one is configured."""
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    def _request_body(
        self, payload: OutboundPayload, *, stream: bool, structured: bool
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._settings.model,
            "messages": _wire_messages(payload),
            "stream": stream,
        }
        if structured:
            # No `strict: true` and no `_strict_schema()` rewrite: those are Groq's
            # documented quirk, and this server has not been observed to need them.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "scheduling_plan",
                    "schema": PlanningOutput.model_json_schema(),
                },
            }
        return body

    @staticmethod
    def _content(response: object) -> str:
        if not isinstance(response, dict):
            raise LocalModelUnavailableError("the local model returned an invalid response")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise LocalModelUnavailableError("the local model returned an invalid response")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LocalModelUnavailableError("the local model returned an invalid response")
        return message["content"]

    @staticmethod
    def _stream_content(event: object) -> str:
        if not isinstance(event, dict):
            raise ValueError("stream event must be an object")
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("stream event has no choice")
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            raise ValueError("stream event has no delta")
        content = delta.get("content", "")
        if not isinstance(content, str):
            raise ValueError("stream content must be text")
        return content


def _wire_messages(payload: OutboundPayload) -> list[dict[str, str]]:
    """Fold `scheduling_context`/`scheduling_rules` into the system message text.

    Same shaping as `HttpGroqClient._wire_messages` and for the same reason, restated
    here rather than shared because request shaping belongs to the adapter: a
    chat-completions endpoint only ever feeds `messages[].content` to the model, so a
    sibling top-level key would not reach it whether or not the server rejects it (see
    that method's docstring and evidence/TICK-039 for the live confirmation on Groq).
    `OutboundPayload.messages` stays exactly the system+user pair the privacy gate
    validates.
    """
    system_message, user_message = payload.messages
    system_content = (
        f"{system_message.content}\n\n"
        f"scheduling_context: {payload.scheduling_context.model_dump_json()}\n\n"
        f"scheduling_rules: {payload.scheduling_rules.model_dump_json()}"
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_message.content},
    ]
