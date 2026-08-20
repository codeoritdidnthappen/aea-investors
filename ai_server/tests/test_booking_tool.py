"""Unit tests for the `AuthoritativeTool` that wires real booking into chat (TICK-034).

`BookingTool` is exercised here against a fake `BookingService` (the ticket's own
"Unit-test the new tool against a fake `BookingService`" requirement) for success,
conflict, and missing-slot-token cases, plus the existing reschedule/cancel fallback.
`BookingToolSettings` and `NoMappedCandidateSource` are the small pieces of
configuration/wiring this ticket adds around it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai_server.app.chat import BookingTool, BookingToolSettings, NoMappedCandidateSource
from ai_server.llm.groq import PlanningOutput
from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError
from ai_server.scheduling.booking import AppointmentRequest, BookedAppointment, SlotBookingError

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
REQUEST = AppointmentRequest(
    category_id="5", title="Office Visit", facility_id="9", billing_location_id="10"
)


class FakeBookingService:
    """A `BookingService`-shaped double that returns or raises exactly what it's told."""

    def __init__(self, outcome: BookedAppointment | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str, AppointmentRequest, datetime]] = []

    async def book(
        self,
        access_token: str,
        patient_id: str,
        slot_token: str,
        request: AppointmentRequest,
        now: datetime,
    ) -> BookedAppointment:
        self.calls.append((access_token, patient_id, slot_token, request, now))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def run(coroutine):
    return asyncio.run(coroutine)


def tool(
    booking: FakeBookingService,
    access_token: str | None = "token",
    patient_id: str | None = "patient-uuid",
) -> BookingTool:
    return BookingTool(
        booking=booking,  # type: ignore[arg-type]
        appointment_request=REQUEST,
        access_token=access_token,
        patient_id=patient_id,
        now=NOW,
    )


# --- AC3: a `book` plan with a `slot_token` executes through `BookingService` -------


def test_book_success_produces_a_public_summary_of_only_the_openemr_confirmed_outcome() -> None:
    booked = BookedAppointment(
        id="501", starts_at=NOW + timedelta(hours=2), ends_at=NOW + timedelta(hours=2, minutes=30)
    )
    booking = FakeBookingService(booked)
    plan = PlanningOutput(intent="book", slot_token="slot_abc123")

    result = run(tool(booking).execute(plan))

    assert booking.calls == [("token", "patient-uuid", "slot_abc123", REQUEST, NOW)]
    assert "501" in result.public_summary
    assert booked.starts_at.isoformat() in result.public_summary
    assert booked.ends_at.isoformat() in result.public_summary
    # AC4 (FR-20): only the OpenEMR-confirmed outcome, no invented commitment language.
    assert "confirmed" in result.public_summary.lower()


def test_book_conflict_or_stale_slot_is_reported_clearly_with_no_invented_commitment() -> None:
    booking = FakeBookingService(SlotBookingError("slot token is unknown or already used"))
    plan = PlanningOutput(intent="book", slot_token="slot_stale")

    result = run(tool(booking).execute(plan))

    assert "no longer available" in result.public_summary.lower()
    assert "booked" not in result.public_summary.lower()
    assert "confirmed" not in result.public_summary.lower()


def test_book_openemr_request_failure_reports_no_appointment_was_created() -> None:
    booking = FakeBookingService(
        OpenEmrRequestError("OpenEMR booking request failed with status 409")
    )
    plan = PlanningOutput(intent="book", slot_token="slot_conflict")

    result = run(tool(booking).execute(plan))

    assert "no appointment was created" in result.public_summary.lower()
    assert "confirmed" not in result.public_summary.lower()


def test_book_intent_missing_a_slot_token_falls_back_to_the_no_action_summary() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="book", slot_token=None)

    result = run(tool(booking).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


def test_book_with_no_delegated_access_token_falls_back_without_calling_booking() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="book", slot_token="slot_abc123")

    result = run(tool(booking, access_token=None).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


def test_book_with_no_bound_patient_id_falls_back_without_calling_booking() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="book", slot_token="slot_abc123")

    result = run(tool(booking, patient_id=None).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


# --- AC3: reschedule/cancel fall back to the existing no-action summary -------------


def test_reschedule_intent_falls_back_to_the_no_action_summary() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="reschedule")

    result = run(tool(booking).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


def test_cancel_intent_falls_back_to_the_no_action_summary() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="cancel")

    result = run(tool(booking).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


def test_information_intent_falls_back_to_the_no_action_summary() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="information")

    result = run(tool(booking).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


# --- BookingToolSettings: admin-configured appointment fields -----------------------


def test_booking_tool_settings_requires_every_field_except_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AI_BOOKING_CATEGORY_ID",
        "AI_BOOKING_TITLE",
        "AI_BOOKING_FACILITY_ID",
        "AI_BOOKING_BILLING_LOCATION_ID",
        "AI_BOOKING_PROVIDER_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(OpenEmrConfigurationError, match="AI_BOOKING_CATEGORY_ID"):
        BookingToolSettings.from_environment()

    monkeypatch.setenv("AI_BOOKING_CATEGORY_ID", "5")
    monkeypatch.setenv("AI_BOOKING_TITLE", "Office Visit")
    monkeypatch.setenv("AI_BOOKING_FACILITY_ID", "9")
    monkeypatch.setenv("AI_BOOKING_BILLING_LOCATION_ID", "10")

    settings = BookingToolSettings.from_environment()

    assert settings.appointment_request() == REQUEST


def test_booking_tool_settings_includes_an_optional_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BOOKING_CATEGORY_ID", "5")
    monkeypatch.setenv("AI_BOOKING_TITLE", "Office Visit")
    monkeypatch.setenv("AI_BOOKING_FACILITY_ID", "9")
    monkeypatch.setenv("AI_BOOKING_BILLING_LOCATION_ID", "10")
    monkeypatch.setenv("AI_BOOKING_PROVIDER_ID", "3")

    settings = BookingToolSettings.from_environment()

    assert settings.appointment_request().provider_id == "3"


# --- NoMappedCandidateSource: honest zero-candidates, never an invented default -----


def test_no_mapped_candidate_source_reports_no_candidates() -> None:
    result = run(NoMappedCandidateSource().candidate_slots())

    assert result == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
