"""Regression tests for TICK-039: `scheduling_context`/`scheduling_rules` must
actually reach Groq's real wire request, not just the internal `OutboundPayload`.

Live evidence (`evidence/TICK-039/GROQ_REQUEST_EVIDENCE.md`) proved the bug and the
fix against the real Groq API: the pre-fix `HttpGroqClient._request_body` sent only
`model`/`messages`/`stream`, so Groq correctly planned `intent="cancel"` from the
user's text but always returned `appointment_token=null` -- exactly why
`BookingTool.execute()` (`chat.py:127`) always fell through to `NoActionTool`. A
literal fix matching `ARCHITECTURE.md`'s "Approved external request shape" (sibling
top-level `scheduling_context`/`scheduling_rules` keys) was live-confirmed to make
Groq 400 ("property 'scheduling_context' is unsupported") -- unknown top-level
request properties are rejected outright, and even if they were not, an
OpenAI-compatible completions endpoint only ever feeds `messages[].content` to the
model. The actual fix folds that JSON into the system message content instead,
leaving `OutboundPayload.messages` (and everything that already depends on its fixed
system+user shape, e.g. the privacy gate) untouched.

These tests exercise `HttpGroqClient` -- previously entirely untested at the wire
level, which is exactly how this bug went unnoticed since TICK-010 -- against
`httpx.MockTransport` (this suite's existing stand-in for a live HTTP peer, per
every OpenEMR-adapter test), plus one full `GroqWorkflow` -> `BookingTool` ->
`CancellationService` pipeline test proving the fix actually unblocks cancellation.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from ai_server.app.chat import SYSTEM_PROMPT, BookingTool
from ai_server.llm.groq import GroqSettings, GroqWorkflow, HttpGroqClient
from ai_server.privacy.gate import (
    AnonymousAppointment,
    OutboundMessage,
    OutboundPayload,
    PrivacyGate,
    ResponseFormat,
    SchedulingContext,
    SchedulingRules,
)
from ai_server.scheduling.booking import AppointmentRequest
from ai_server.scheduling.cancel import CancelledAppointment

NOW = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
APPOINTMENT_TOKEN = "appt_repro0000000000000000000001"


def settings() -> GroqSettings:
    return GroqSettings(api_key="test-groq-key", zdr_verified_on=date(2026, 8, 18))


def reproducing_payload() -> OutboundPayload:
    """The exact reproducing turn from TICK-039's Context: a real appointment token
    in `scheduling_context.current_appointments`, cancellation enabled, and a
    cancellation request in the user's own words."""
    return OutboundPayload(
        model="openai/gpt-oss-120b",
        messages=[
            OutboundMessage(role="system", content=SYSTEM_PROMPT),
            OutboundMessage(role="user", content="Can you cancel my upcoming appointment?"),
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


def run(coroutine):
    return asyncio.run(coroutine)


def sse_response(text: str) -> httpx.Response:
    """A minimal single-chunk SSE response shaped like Groq's real streaming replies."""
    event = json.dumps({"choices": [{"delta": {"content": text}}]})
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"data: {event}\n\ndata: [DONE]\n\n".encode(),
    )


# --- AC1/AC2 regression: the real appointment token reaches Groq's wire request ----


def test_ac1_planning_request_never_sends_scheduling_context_as_a_top_level_key() -> None:
    """Groq's real API 400s on an unrecognized top-level property (confirmed live,
    evidence/TICK-039/GROQ_REQUEST_EVIDENCE.md section 2) -- the literal
    ARCHITECTURE.md example shape must never be sent as-is."""
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
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

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpGroqClient(settings(), http_client).complete(reproducing_payload())

    run(scenario())

    assert len(captured) == 1
    body = captured[0]
    assert set(body.keys()) == {"model", "messages", "stream", "response_format"}
    assert "scheduling_context" not in body
    assert "scheduling_rules" not in body


def test_ac1_planning_request_delivers_the_real_appointment_token_in_message_content() -> None:
    """The only channel Groq's completions endpoint actually reads is
    `messages[].content` -- the real appointment_token must appear there."""
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
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

    async def scenario() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            return await HttpGroqClient(settings(), http_client).complete(reproducing_payload())

    run(scenario())

    messages = captured[0]["messages"]
    assert len(messages) == 2
    system_message, user_message = messages
    assert system_message["role"] == "system"
    assert system_message["content"].startswith(SYSTEM_PROMPT)
    assert APPOINTMENT_TOKEN in system_message["content"]
    assert "cancellation_enabled" in system_message["content"]
    # The user message is untouched -- only the system message carries the context.
    assert user_message == {"role": "user", "content": "Can you cancel my upcoming appointment?"}


def test_ac1_final_streaming_request_also_omits_unsupported_top_level_keys() -> None:
    """The post-tool streaming call goes through the same `_request_body`; it must
    stay 400-safe too."""
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(b'data: {"choices":[{"delta":{"content":"Cancelled."}}]}\n\ndata: [DONE]\n\n'),
        )

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = HttpGroqClient(settings(), http_client)
            return [chunk async for chunk in client.stream(reproducing_payload())]

    chunks = run(scenario())

    assert chunks == ["Cancelled."]
    assert set(captured[0].keys()) == {"model", "messages", "stream"}


# --- Full pipeline: fixed Groq wiring actually unblocks cancellation ---------------


@dataclass
class FakeCancellationService:
    outcome: CancelledAppointment
    calls: list[tuple[str, str, str, datetime]]

    async def cancel(
        self, access_token: str, patient_id: str, appointment_token: str, now: datetime
    ) -> CancelledAppointment:
        self.calls.append((access_token, patient_id, appointment_token, now))
        return self.outcome


@dataclass
class RefusingBookingService:
    async def book(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("booking must not be called for a cancel-only scenario")


@dataclass
class RefusingRescheduleService:
    async def reschedule(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("reschedule must not be called for a cancel-only scenario")


def test_ac2_a_groq_shaped_response_with_the_real_token_reaches_cancellation_service() -> None:
    """End to end through the real `GroqWorkflow`/`HttpGroqClient`/`BookingTool`:
    a Groq peer that only resolves an `appointment_token` when it can see
    `scheduling_context` in the request (mirroring the real, live-confirmed Groq
    behavior) must result in `CancellationService.cancel()` being invoked with that
    token -- proving TICK-039's fix, not just the wiring TICK-036 already covered."""

    async def groq_peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["stream"]:
            # The post-tool final response call: stream back the tool's own summary.
            return sse_response(body["messages"][1]["content"])
        system_content = body["messages"][0]["content"]
        if APPOINTMENT_TOKEN in system_content and "cancellation_enabled" in system_content:
            token = APPOINTMENT_TOKEN
        else:
            token = None  # Exactly the pre-fix, live-observed Groq behavior.
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"intent": "cancel", "slot_token": None, "appointment_token": token}
                            )
                        }
                    }
                ]
            },
        )

    cancellation = FakeCancellationService(
        outcome=CancelledAppointment(id="501", status="cancelled"), calls=[]
    )

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(groq_peer)) as http_client:
            groq_client = HttpGroqClient(settings(), http_client)
            workflow = GroqWorkflow(PrivacyGate.create(), groq_client)
            tool = BookingTool(
                booking=RefusingBookingService(),  # type: ignore[arg-type]
                cancellation=cancellation,  # type: ignore[arg-type]
                reschedule=RefusingRescheduleService(),  # type: ignore[arg-type]
                appointment_request=AppointmentRequest(
                    category_id="5", title="Office Visit", facility_id="9", billing_location_id="10"
                ),
                access_token="patient-access-token",
                patient_id="patient-uuid",
                now=NOW,
            )
            return [chunk async for chunk in workflow.respond(reproducing_payload(), tool)]

    chunks = run(scenario())

    assert cancellation.calls == [("patient-access-token", "patient-uuid", APPOINTMENT_TOKEN, NOW)]
    assert (
        "".join(chunks) == "Cancelled and confirmed by OpenEMR: appointment 501 is now cancelled."
    )


def test_ac3_without_scheduling_context_in_the_request_no_action_is_the_honest_outcome() -> None:
    """The inverse of the above: if the request genuinely carries no matching
    appointment (mirrors the pre-fix wire body), `NoActionTool`'s fallback is the
    honest outcome, not a bug -- `NoActionTool` must stay reserved for turns that
    truly have nothing to act on (TICK-039 AC3)."""

    async def groq_peer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["stream"]:
            return sse_response(body["messages"][1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"intent": "cancel", "slot_token": None, "appointment_token": None}
                            )
                        }
                    }
                ]
            },
        )

    cancellation = FakeCancellationService(
        outcome=CancelledAppointment(id="501", status="cancelled"), calls=[]
    )
    empty_payload = reproducing_payload().model_copy(
        update={
            "scheduling_context": reproducing_payload().scheduling_context.model_copy(
                update={"current_appointments": []}
            )
        }
    )

    async def scenario() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(groq_peer)) as http_client:
            groq_client = HttpGroqClient(settings(), http_client)
            workflow = GroqWorkflow(PrivacyGate.create(), groq_client)
            tool = BookingTool(
                booking=RefusingBookingService(),  # type: ignore[arg-type]
                cancellation=cancellation,  # type: ignore[arg-type]
                reschedule=RefusingRescheduleService(),  # type: ignore[arg-type]
                appointment_request=AppointmentRequest(
                    category_id="5", title="Office Visit", facility_id="9", billing_location_id="10"
                ),
                access_token="patient-access-token",
                patient_id="patient-uuid",
                now=NOW,
            )
            return [chunk async for chunk in workflow.respond(empty_payload, tool)]

    chunks = run(scenario())

    assert cancellation.calls == []
    assert "".join(chunks) == "No scheduling action is available yet in this demo."


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
