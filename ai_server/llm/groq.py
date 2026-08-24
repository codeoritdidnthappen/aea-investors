"""Groq, narrowed to answering one restated general-knowledge question.

TICK-064, from `docs/LOCAL_LLM_SPEC.md` D13: Groq no longer plans anything. It used to
receive the patient's typed message plus `scheduling_context`/`scheduling_rules` and
return a `PlanningOutput` that chose the turn's action; the local model owns all of that
now (D9, D13), so `PlanningOutput`, `GroqWorkflow`, `AuthoritativeTool` and the
scheduling-context wire folding are gone with it.

What remains is a transport: post one `OutboundPayload` -- two messages this codebase
composed, screened by `ai_server.privacy.gate.OutboundDispatcher` first -- and return
the prose answer. There is no structured mode any more, because a general-knowledge
answer is a paragraph rather than a schema, and no streaming, because the answer is
folded into a reply that `ModelTurnService` chunks for the browser itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

import httpx

from ai_server.llm.provider import LlmUnavailableError
from ai_server.privacy.gate import OutboundPayload

GROQ_MODEL = "openai/gpt-oss-120b"


class GroqConfigurationError(Exception):
    """Raised when a required local Groq deployment control is absent."""


class GroqUnavailableError(LlmUnavailableError):
    """Raised when Groq cannot provide a safe response.

    A subclass of the provider-agnostic error since TICK-058, so a caller can degrade on
    any configured provider's unavailability.
    """


@dataclass(frozen=True)
class GroqSettings:
    """Validated server-side credentials and retention-control evidence."""

    api_key: str
    zdr_verified_on: date
    endpoint: str = "https://api.groq.com/openai/v1/chat/completions"

    @classmethod
    def from_environment(cls) -> GroqSettings:
        """Parse the required Groq settings once during application startup."""
        api_key = os.environ.get("GROQ_API_KEY")
        verification = os.environ.get("GROQ_ZDR_VERIFIED_ON")
        if not api_key:
            raise GroqConfigurationError("GROQ_API_KEY is required")
        if not verification:
            raise GroqConfigurationError("GROQ_ZDR_VERIFIED_ON is required before Groq traffic")
        try:
            verified_on = date.fromisoformat(verification)
        except ValueError as exc:
            raise GroqConfigurationError("GROQ_ZDR_VERIFIED_ON must be an ISO date") from exc
        return cls(api_key=api_key, zdr_verified_on=verified_on)


class HttpGroqClient:
    """OpenAI-compatible Groq transport; credentials never leave this server.

    Satisfies `ai_server.privacy.gate.ExternalModelClient`, and deliberately nothing
    else. It has no `tool_call()`, so it cannot satisfy `ModelTurnService`'s
    `ToolCallClient` even by accident: an external model can never be the front door for
    a turn (D3, FR-34).
    """

    def __init__(self, settings: GroqSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def complete(self, payload: OutboundPayload) -> str:
        """Return Groq's prose answer to an already screened, already composed payload."""
        try:
            response = await self._client.post(
                self._settings.endpoint,
                headers=self._headers(),
                json=self._request_body(payload),
            )
        except httpx.HTTPError as exc:
            raise GroqUnavailableError("Groq is unavailable") from exc
        if response.status_code != 200:
            raise GroqUnavailableError("Groq is unavailable")
        try:
            return self._content(response.json())
        except ValueError as exc:
            raise GroqUnavailableError("Groq returned an invalid response") from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _request_body(payload: OutboundPayload) -> dict[str, object]:
        """Shape the request body.

        A straight serialisation of `payload.messages` now. TICK-039's fold of
        `scheduling_context`/`scheduling_rules` into the system message text is gone
        because the data is gone: there is no scheduling context outbound any more (D13),
        so the live Groq finding that motivated the fold -- a 400 on any unrecognised
        top-level property, and an OpenAI-compatible endpoint only ever feeding
        `messages[].content` to the model -- no longer has anything to apply to.
        """
        return {
            "model": GROQ_MODEL,
            "messages": [message.model_dump() for message in payload.messages],
            "stream": False,
        }

    @staticmethod
    def _content(response: object) -> str:
        if not isinstance(response, dict):
            raise GroqUnavailableError("Groq returned an invalid response")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise GroqUnavailableError("Groq returned an invalid response")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise GroqUnavailableError("Groq returned an invalid response")
        return message["content"]
