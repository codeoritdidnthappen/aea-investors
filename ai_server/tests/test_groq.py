from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

import pytest

from ai_server.llm.groq import (
    GROQ_MODEL,
    UNAVAILABLE_RESPONSE,
    AuthoritativeToolResult,
    GroqConfigurationError,
    GroqSettings,
    GroqUnavailableError,
    GroqWorkflow,
    PlanningOutput,
)
from ai_server.privacy.gate import OutboundPayload, PrivacyGate


def payload() -> OutboundPayload:
    return OutboundPayload.model_validate(
        {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": "Plan an anonymous scheduling action."},
                {"role": "user", "content": "Please find an appointment."},
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


def test_ticket_010_validates_plan_before_authoritative_tool_and_streams_afterward() -> None:
    client = CapturingGroqClient('{"intent":"book","slot_token":"slot_demo"}')
    tool = CapturingTool(calls=[])

    chunks = collect(GroqWorkflow(PrivacyGate.create(), client), payload(), tool)

    assert chunks == ["Confirmed ", "by OpenEMR."]
    assert tool.calls == [PlanningOutput(intent="book", slot_token="slot_demo")]
    assert [request.model for request in client.calls] == [GROQ_MODEL, GROQ_MODEL]
    assert (
        client.calls[0].scheduling_context.model_dump() == payload().scheduling_context.model_dump()
    )
    assert client.calls[1].messages[1].content == "OpenEMR reports the appointment is booked."


def test_ticket_010_invalid_plan_never_calls_tool_or_streams() -> None:
    client = CapturingGroqClient('{"intent":"invented","slot_token":"slot_demo"}')
    tool = CapturingTool(calls=[])

    assert collect(GroqWorkflow(PrivacyGate.create(), client), payload(), tool) == [
        UNAVAILABLE_RESPONSE
    ]
    assert tool.calls == []
    assert len(client.calls) == 1


def test_ticket_010_model_failure_makes_no_appointment_claim() -> None:
    client = CapturingGroqClient(fail_planning=True)
    tool = CapturingTool(calls=[])

    assert collect(GroqWorkflow(PrivacyGate.create(), client), payload(), tool) == [
        UNAVAILABLE_RESPONSE
    ]
    assert tool.calls == []


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
