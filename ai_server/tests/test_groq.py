from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

import httpx
import pytest

from ai_server.llm.groq import (
    GROQ_MODEL,
    PLANNING_FAILED_RESPONSE,
    AuthoritativeToolResult,
    GroqConfigurationError,
    GroqSettings,
    GroqUnavailableError,
    GroqWorkflow,
    PlanningOutput,
)
from ai_server.privacy.gate import OutboundPayload, PrivacyGate


def payload(user_message: str = "Please find an appointment.") -> OutboundPayload:
    return OutboundPayload.model_validate(
        {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "Plan an anonymous scheduling action."},
                {"role": "user", "content": user_message},
            ],
            "scheduling_context": {
                "current_datetime": "2026-08-18T14:30:00-05:00",
                "timezone": "America/Chicago",
                "office_hours": [],
                "closures": [],
                "open_slots": [],
            },
            "scheduling_rules": {
                "minimum_booking_notice_minutes": 1440,
                "booking_enabled": True,
                "rescheduling_enabled": True,
                "cancellation_enabled": True,
            },
            "response_format": {"type": "json_schema", "schema_version": "1"},
        }
    )


class CapturingGroqClient:
    def __init__(
        self,
        plan: str = '{"intent":"information","slot_token":null}',
        fail_planning: bool = False,
    ) -> None:
        self.plan = plan
        self.fail_planning = fail_planning
        self.calls: list[OutboundPayload] = []
        self.final_chunks = ["Confirmed ", "by OpenEMR."]

    async def complete(self, request: OutboundPayload) -> str:
        self.calls.append(request)
        if self.fail_planning:
            raise GroqUnavailableError("offline")
        return self.plan

    async def _stream(self, request: OutboundPayload) -> AsyncIterator[str]:
        self.calls.append(request)
        for chunk in self.final_chunks:
            yield chunk

    def stream(self, request: OutboundPayload) -> AsyncIterator[str]:
        return self._stream(request)


@dataclass
class RaisingGroqClient:
    """A planning client whose `complete` raises one specific transport error.

    `CapturingGroqClient` covers `GroqUnavailableError` (`fail_planning`) and
    `ValidationError` (an unparseable plan); this covers the third caught planning
    failure, a raw `httpx.HTTPError` escaping the client (TICK-048).
    """

    error: Exception

    async def complete(self, request: OutboundPayload) -> str:
        del request
        raise self.error

    def stream(self, request: OutboundPayload) -> AsyncIterator[str]:
        del request
        raise AssertionError("stream must never run after planning fails")


@dataclass
class CapturingTool:
    calls: list[PlanningOutput]

    async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
        self.calls.append(plan)
        return AuthoritativeToolResult(public_summary="OpenEMR reports the appointment is booked.")


def collect(
    workflow: GroqWorkflow, client_payload: OutboundPayload, tool: CapturingTool
) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in workflow.respond(client_payload, tool)]

    return asyncio.run(run())


def test_ticket_010_requires_dated_zdr_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GROQ_ZDR_VERIFIED_ON", raising=False)

    with pytest.raises(GroqConfigurationError, match="ZDR"):
        GroqSettings.from_environment()

    monkeypatch.setenv("GROQ_ZDR_VERIFIED_ON", "2026-08-18")
    assert GroqSettings.from_environment().zdr_verified_on == date(2026, 8, 18)


def test_ticket_010_validates_plan_before_authoritative_tool_and_yields_its_result() -> None:
    client = CapturingGroqClient('{"intent":"book","slot_token":"slot_demo"}')
    tool = CapturingTool(calls=[])

    chunks = collect(GroqWorkflow(PrivacyGate.create(), client), payload(), tool)

    assert chunks == ["OpenEMR reports the appointment is booked."]
    assert tool.calls == [PlanningOutput(intent="book", slot_token="slot_demo")]
    # Only the planning call happens -- no second Groq call describes the result
    # (TICK-041): the tool's own `public_summary` is yielded verbatim instead.
    assert [request.model for request in client.calls] == [GROQ_MODEL]
    assert (
        client.calls[0].scheduling_context.model_dump() == payload().scheduling_context.model_dump()
    )


def test_tick041_a_second_model_call_never_happens_so_it_cannot_fabricate_a_result() -> None:
    """Regression for the live-found bug: a second Groq call used to "describe" the
    tool's honest result, and its *unchecked* streamed output -- not the tool's real
    result -- was what the patient saw. Here `client.stream()` is rigged to fabricate
    a success claim completely unrelated to the tool's real (failure) outcome; if
    `GroqWorkflow` ever called it again, this test would see that fabrication instead
    of the honest text."""
    client = CapturingGroqClient('{"intent":"cancel","appointment_token":"appt_demo"}')
    client.final_chunks = ['{"appointment_token":"appt_demo","cancellation":"confirmed"}']

    @dataclass
    class FailingTool:
        async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
            del plan
            return AuthoritativeToolResult(
                public_summary=(
                    "OpenEMR could not confirm that cancellation just now, so the "
                    "appointment was not cancelled. Please try again."
                )
            )

    chunks = collect(GroqWorkflow(PrivacyGate.create(), client), payload(), FailingTool())

    assert chunks == [
        "OpenEMR could not confirm that cancellation just now, so the "
        "appointment was not cancelled. Please try again."
    ]
    assert "confirmed" not in "".join(chunks)
    # `stream()` was never invoked -- only the one planning call happened.
    assert len(client.calls) == 1


def test_ticket_010_invalid_plan_never_calls_tool_or_streams() -> None:
    client = CapturingGroqClient('{"intent":"invented","slot_token":"slot_demo"}')
    tool = CapturingTool(calls=[])

    assert collect(GroqWorkflow(PrivacyGate.create(), client), payload(), tool) == [
        PLANNING_FAILED_RESPONSE
    ]
    assert tool.calls == []
    assert len(client.calls) == 1


def test_ticket_010_model_failure_makes_no_appointment_claim() -> None:
    client = CapturingGroqClient(fail_planning=True)
    tool = CapturingTool(calls=[])

    assert collect(GroqWorkflow(PrivacyGate.create(), client), payload(), tool) == [
        PLANNING_FAILED_RESPONSE
    ]
    assert tool.calls == []


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(CapturingGroqClient(fail_planning=True), id="groq_unavailable_error"),
        pytest.param(CapturingGroqClient('{"intent":"invented"}'), id="validation_error"),
        pytest.param(RaisingGroqClient(httpx.ConnectError("no route to Groq")), id="http_error"),
    ],
)
def test_ticket_048_planning_failure_never_blames_scheduling(client: object) -> None:
    """Every planning failure yields the neutral per-turn message, even when the turn
    was not about scheduling at all (the reported case: an address change)."""
    tool = CapturingTool(calls=[])

    chunks = collect(
        GroqWorkflow(PrivacyGate.create(), client),  # type: ignore[arg-type]
        payload("I need to change my address."),
        tool,
    )

    assert chunks == [PLANNING_FAILED_RESPONSE]
    assert tool.calls == []


def test_ticket_048_planning_failure_message_is_neutral_and_actionable() -> None:
    text = PLANNING_FAILED_RESPONSE.lower()

    # It never claims scheduling specifically, and never sends a patient portal user
    # to OpenEMR's staff-only native scheduling screen.
    assert "scheduling" not in text
    assert "openemr" not in text
    # It says the assistant could not handle the request, and what to do next.
    assert "could not work out how to handle that request" in text
    assert "contact the clinic" in text


def test_ticket_010_privacy_rejection_never_calls_groq() -> None:
    client = CapturingGroqClient()
    tool = CapturingTool(calls=[])
    rejected = OutboundPayload.model_validate(
        {
            **payload().model_dump(mode="json"),
            "messages": [
                payload().messages[0].model_dump(),
                {"role": "user", "content": "My phone is 555-555-5555."},
            ],
        }
    )

    assert collect(GroqWorkflow(PrivacyGate.create(), client), rejected, tool) == [
        "Please remove personal or health information and try again."
    ]
    assert client.calls == []
    assert tool.calls == []
