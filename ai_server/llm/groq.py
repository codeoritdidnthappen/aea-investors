"""Groq planning behind the local privacy gate; an authoritative tool's own result is
always yielded verbatim afterward, never re-described by a second Groq call
(TICK-041)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import AsyncIterator, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_server.privacy.gate import (
    LOCAL_CORRECTION,
    OutboundPayload,
    PrivacyGate,
)

GROQ_MODEL = "openai/gpt-oss-120b"
# Planning failed for *this* turn (TICK-048). The turn is not necessarily a scheduling
# turn -- a patient can ask about an address change, records, or billing, and the
# planner cannot express any of those -- so this must not claim scheduling is what
# broke, and must not point at OpenEMR's native scheduling screen, which is a staff
# screen no patient portal user can reach. `ChatService` has its own, distinct string
# for the unrelated "Groq is not configured at all" deployment fault.
PLANNING_FAILED_RESPONSE = (
    "Sorry -- I could not work out how to handle that request, so nothing was done. "
    "Please try rewording it, or contact the clinic directly if it is something this "
    "chat cannot do."
)


class GroqConfigurationError(Exception):
    """Raised when a required local Groq deployment control is absent."""


class GroqUnavailableError(Exception):
    """Raised when Groq cannot provide a safe response."""


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


class PlanningOutput(BaseModel):
    """The complete model contract accepted before an authoritative tool can run."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["information", "book", "reschedule", "cancel"]
    slot_token: str | None = Field(default=None, pattern="^slot_[A-Za-z0-9_-]{1,64}$")
    appointment_token: str | None = Field(default=None, pattern="^appt_[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class AuthoritativeToolResult:
    """A locally verified outcome which may be described after tool completion."""

    public_summary: str


class AuthoritativeTool(Protocol):
    """The OpenEMR-facing boundary for a validated plan."""

    async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
        """Return only a result already validated by the authoritative system."""


class GroqClient(Protocol):
    """The intentionally small Groq boundary used by the workflow."""

    async def complete(self, payload: OutboundPayload) -> str:
        """Return a structured planning response."""

    def stream(self, payload: OutboundPayload) -> AsyncIterator[str]:
        """Stream a final response after an authoritative tool result exists."""


def _strict_schema(schema: dict[str, object]) -> dict[str, object]:
    """Satisfy Groq/OpenAI strict structured-output mode's `required` rule.

    Strict mode has no notion of an optional property (confirmed live: Groq 400s
    with "every key in properties" must be `required`, even though Pydantic only
    lists non-default fields there); a nullable type union is how an optional
    field is expressed instead, which PlanningOutput's `slot_token` already is.
    """
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema = {**schema, "required": list(properties.keys())}
    return schema


class HttpGroqClient:
    """OpenAI-compatible Groq transport; credentials never leave this server."""

    def __init__(self, settings: GroqSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def complete(self, payload: OutboundPayload) -> str:
        """Request a JSON plan from Groq's pinned model."""
        try:
            response = await self._client.post(
                self._settings.endpoint,
                headers=self._headers(),
                json=self._request_body(payload, stream=False, structured=True),
            )
        except httpx.HTTPError as exc:
            raise GroqUnavailableError("Groq planning is unavailable") from exc
        if response.status_code != 200:
            raise GroqUnavailableError("Groq planning is unavailable")
        try:
            return self._content(response.json())
        except ValueError as exc:
            raise GroqUnavailableError("Groq returned an invalid planning response") from exc

    async def _stream(self, payload: OutboundPayload) -> AsyncIterator[str]:
        """Yield Groq SSE content chunks without buffering the final response."""
        try:
            async with self._client.stream(
                "POST",
                self._settings.endpoint,
                headers=self._headers(),
                json=self._request_body(payload, stream=True, structured=False),
            ) as response:
                if response.status_code != 200:
                    raise GroqUnavailableError("Groq response streaming is unavailable")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ")
                    if data == "[DONE]":
                        return
                    try:
                        chunk = self._stream_content(json.loads(data))
                    except (TypeError, ValueError, KeyError) as exc:
                        raise GroqUnavailableError(
                            "Groq returned an invalid streamed response"
                        ) from exc
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise GroqUnavailableError("Groq response streaming is unavailable") from exc

    def stream(self, payload: OutboundPayload) -> AsyncIterator[str]:
        """Return the asynchronous final-response stream."""
        return self._stream(payload)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _request_body(
        payload: OutboundPayload, *, stream: bool, structured: bool
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": GROQ_MODEL,
            "messages": HttpGroqClient._wire_messages(payload),
            "stream": stream,
        }
        if structured:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "scheduling_plan",
                    "strict": True,
                    "schema": _strict_schema(PlanningOutput.model_json_schema()),
                },
            }
        return body

    @staticmethod
    def _wire_messages(payload: OutboundPayload) -> list[dict[str, str]]:
        """Fold `scheduling_context`/`scheduling_rules` into the system message text
        actually sent to Groq (TICK-039).

        `OutboundPayload.messages` is the architecture-approved *internal* contract --
        exactly a system then a user message -- that the privacy gate and every other
        caller of this payload still rely on unchanged. `scheduling_context`/
        `scheduling_rules` live as separate top-level fields on that same payload
        (mirroring ARCHITECTURE.md's "Approved external request shape" example), but
        two live findings ruled out sending them as sibling top-level JSON keys in the
        actual Groq request body: Groq's real chat completions endpoint 400s on any
        top-level property it does not recognize (confirmed live: `{"error":{"message":
        "property 'scheduling_context' is unsupported", ...}}`), and even if it
        tolerated them, an OpenAI-compatible completions endpoint only ever feeds
        `messages[].content` to the model -- an unrecognized sibling key is never part
        of the prompt regardless. This was the actual root cause of TICK-039: with only
        the fixed `messages` previously sent, Groq correctly planned `intent="cancel"`
        from the user's text but always returned `appointment_token=null` because it
        was never given `current_appointments` to select from, so `BookingTool.
        execute()`'s `plan.appointment_token is not None` check always fell through to
        `NoActionTool` -- not a prompt-following gap. Appending the same data as JSON
        text on the system message is the one channel that reaches the model;
        confirmed live it lets the model resolve a real appointment_token.
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

    @staticmethod
    def _content(response: object) -> str:
        if not isinstance(response, dict):
            raise GroqUnavailableError("Groq returned an invalid planning response")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise GroqUnavailableError("Groq returned an invalid planning response")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise GroqUnavailableError("Groq returned an invalid planning response")
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


class GroqWorkflow:
    """Gate Groq calls and sequence plan, authoritative tool, then final streaming."""

    def __init__(self, gate: PrivacyGate, client: GroqClient) -> None:
        self._gate = gate
        self._client = client

    async def respond(
        self, payload: OutboundPayload, tool: AuthoritativeTool
    ) -> AsyncIterator[str]:
        """Yield a safe local result, or the authoritative tool's own result verbatim.

        TICK-041: a second Groq call used to run after the tool executed, asking the
        model to "describe" `result.public_summary` in its own words, and that
        *unchecked* model output -- not `result` itself -- was what the patient saw.
        Live-confirmed (a real OpenEMR 404 on a real cancel attempt): the model
        streamed back a fabricated success claim from an honest failure summary,
        despite an explicit "do not add facts" instruction. A prompt cannot close this
        class of bug -- the model can always be asked one more time to misbehave -- so
        this is a code-level guardrail instead: `result.public_summary` is already the
        authoritative, patient-safe sentence (every `AuthoritativeToolResult` in this
        codebase is constructed as exactly that), and it is now yielded directly.
        Groq is never asked to restate an authoritative outcome, so it cannot rephrase
        a failure into a success or vice versa.
        """
        if not self._safe(payload):
            yield LOCAL_CORRECTION
            return
        try:
            plan = PlanningOutput.model_validate_json(await self._client.complete(payload))
        except (GroqUnavailableError, ValidationError, httpx.HTTPError):
            yield PLANNING_FAILED_RESPONSE
            return

        result = await tool.execute(plan)
        yield result.public_summary

    def _safe(self, payload: OutboundPayload) -> bool:
        return not self._gate.has_sensitive_text(payload.user_message_content())
