"""An OpenAI-compatible local model transport for the turn's routing inference.

LOCAL_LLM_SPEC D7: "Ollama locally, vLLM deployed, selected by config from day one".
Ollama serves the ordinary chat-completions shape at `/v1/chat/completions`, so the
whole adapter is the request/response shaping plus its own settings.

`tool_call()` is the entire surface since TICK-064. The `complete()`/`stream()` pair
existed to mirror the old `GroqClient` Protocol for Groq scheduling planning; D13 moved
planning to this model's own tool call and deleted that Protocol, so the two methods
went with it rather than staying as an unused second way to talk to the same server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import httpx

from ai_server.llm.provider import LlmUnavailableError
from ai_server.llm.tools import TOOL_CALL_SCHEMA_NAME

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
    """OpenAI-compatible local transport; satisfies `ModelTurnService.ToolCallClient`.

    Every failure to reach or parse the local server raises
    `LocalModelUnavailableError`, which `ModelTurnService.stream_reply()` catches via
    `LlmUnavailableError` -- so pointing `LLM_PROVIDER` at a server that is not running
    surfaces as the chat's honest unavailable reply at the point of use, not as an
    exception escaping the request handler.
    """

    def __init__(self, settings: LocalModelSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def tool_call(
        self, messages: Sequence[Mapping[str, str]], *, schema: Mapping[str, Any]
    ) -> str:
        """Run one routing inference and return the model's raw text, unparsed.

        `messages` is an arbitrary-length transcript addressed to a configured model id,
        which is why it is not an `OutboundPayload`: that type is the privacy gate's,
        it pins the model to Groq's, it holds exactly two messages, and it composes both
        of them itself. The two describe different destinations and neither shape is
        usable as the other.

        Nothing here decides what leaves the deployment. `messages` is built by
        `ai_server.llm.prompt.render_turn_messages` and is addressed to the *local*
        model, which is allowed to see patient data (D2). The outbound boundary this
        client is on the safe side of is Groq's, and this method never calls it -- the
        one path that does is `ai_server.llm.general_knowledge`.

        `temperature` and `seed` match `scripts/evaluate_acceptance_corpus.run_case`, so
        a turn in production is the same request the corpus scored (D8).
        """
        try:
            response = await self._client.post(
                self._settings.endpoint,
                headers=self._headers(),
                json={
                    "model": self._settings.model,
                    "messages": [dict(message) for message in messages],
                    "stream": False,
                    "temperature": 0,
                    "seed": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": TOOL_CALL_SCHEMA_NAME, "schema": dict(schema)},
                    },
                },
            )
        except httpx.HTTPError as exc:
            raise LocalModelUnavailableError("the local model is unavailable") from exc
        if response.status_code != 200:
            raise LocalModelUnavailableError("the local model is unavailable")
        try:
            return self._content(response.json())
        except ValueError as exc:
            raise LocalModelUnavailableError(
                "the local model returned an invalid tool-call response"
            ) from exc

    def _headers(self) -> dict[str, str]:
        """Return the request headers; the bearer token only when one is configured."""
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

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
