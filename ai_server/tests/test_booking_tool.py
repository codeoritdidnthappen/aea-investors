"""Unit tests for the `AuthoritativeTool` that wires real booking (TICK-034) and
cancellation (TICK-036) into chat.

`BookingTool` is exercised here against a fake `BookingService`/`CancellationService`
(the tickets' own "Unit-test the new tool against a fake ..." requirement) for
success, conflict/stale-token, and missing-token cases, plus the existing reschedule
fallback. `BookingToolSettings` and `NoMappedCandidateSource` are the small pieces of
configuration/wiring TICK-034 adds around it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai_server.app.chat import BookingTool, BookingToolSettings, NoMappedCandidateSource
from ai_server.llm.groq import PlanningOutput
from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError
from ai_server.scheduling.booking import AppointmentRequest, BookedAppointment, SlotBookingError
from ai_server.scheduling.cancel import (
    AppointmentAlreadyCancelledError,
    AppointmentNotFoundError,
    CancelledAppointment,
    StaleAppointmentTokenError,
)

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


class FakeCancellationService:
    """A `CancellationService`-shaped double that returns or raises exactly what it's told."""

    def __init__(self, outcome: CancelledAppointment | Exception) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str, datetime]] = []

    async def cancel(
        self, access_token: str, patient_id: str, appointment_token: str, now: datetime
    ) -> CancelledAppointment:
        self.calls.append((access_token, patient_id, appointment_token, now))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def run(coroutine):
    return asyncio.run(coroutine)


def _refusing_booking() -> FakeBookingService:
    return FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]


def _refusing_cancellation() -> FakeCancellationService:
    return FakeCancellationService(AssertionError("must not be called"))  # type: ignore[arg-type]


def tool(
    booking: FakeBookingService,
    cancellation: FakeCancellationService | None = None,
    access_token: str | None = "token",
    patient_id: str | None = "patient-uuid",
) -> BookingTool:
    return BookingTool(
        booking=booking,  # type: ignore[arg-type]
        cancellation=cancellation or _refusing_cancellation(),  # type: ignore[arg-type]
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


# --- TICK-036 AC4: a `cancel` plan with an `appointment_token` executes through
# `CancellationService` -----------------------------------------------------------


def test_cancel_success_produces_a_public_summary_of_only_the_openemr_confirmed_outcome() -> None:
    cancelled = CancelledAppointment(id="501", status="cancelled")
    cancellation = FakeCancellationService(cancelled)
    plan = PlanningOutput(intent="cancel", appointment_token="appt_abc123")

    result = run(tool(_refusing_booking(), cancellation).execute(plan))

    assert cancellation.calls == [("token", "patient-uuid", "appt_abc123", NOW)]
    assert "501" in result.public_summary
    assert "cancelled" in result.public_summary.lower()
    # FR-20: only the OpenEMR-confirmed outcome, no invented commitment language.
    assert "confirmed" in result.public_summary.lower()


def test_cancel_with_a_stale_or_already_resolved_token_invents_no_commitment() -> None:
    cancellation = FakeCancellationService(
        StaleAppointmentTokenError("appointment token is unknown or already used")
    )
    plan = PlanningOutput(intent="cancel", appointment_token="appt_stale")

    result = run(tool(_refusing_booking(), cancellation).execute(plan))

    assert "no longer valid" in result.public_summary.lower()
    assert "cancelled" not in result.public_summary.lower()
    assert "confirmed" not in result.public_summary.lower()


def test_cancel_of_an_already_cancelled_appointment_invents_no_commitment() -> None:
    cancellation = FakeCancellationService(
        AppointmentAlreadyCancelledError("this appointment is already cancelled")
    )
    plan = PlanningOutput(intent="cancel", appointment_token="appt_already")

    result = run(tool(_refusing_booking(), cancellation).execute(plan))

    assert "already been cancelled" in result.public_summary.lower()
    assert "confirmed" not in result.public_summary.lower()


def test_cancel_openemr_not_found_reports_no_appointment_was_cancelled() -> None:
    cancellation = FakeCancellationService(
        AppointmentNotFoundError("no appointment with that id for this patient")
    )
    plan = PlanningOutput(intent="cancel", appointment_token="appt_missing")

    result = run(tool(_refusing_booking(), cancellation).execute(plan))

    assert "not cancelled" in result.public_summary.lower()
    assert "confirmed" not in result.public_summary.lower()


def test_cancel_openemr_request_failure_reports_no_appointment_was_cancelled() -> None:
    cancellation = FakeCancellationService(
        OpenEmrRequestError("OpenEMR appointment cancellation failed with status 500")
    )
    plan = PlanningOutput(intent="cancel", appointment_token="appt_error")

    result = run(tool(_refusing_booking(), cancellation).execute(plan))

    assert "not cancelled" in result.public_summary.lower()
    assert "confirmed" not in result.public_summary.lower()


def test_cancel_with_no_delegated_access_token_falls_back_without_calling_cancellation() -> None:
    cancellation = _refusing_cancellation()
    plan = PlanningOutput(intent="cancel", appointment_token="appt_abc123")

    result = run(tool(_refusing_booking(), cancellation, access_token=None).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."


def test_cancel_with_no_bound_patient_id_falls_back_without_calling_cancellation() -> None:
    cancellation = _refusing_cancellation()
    plan = PlanningOutput(intent="cancel", appointment_token="appt_abc123")

    result = run(tool(_refusing_booking(), cancellation, patient_id=None).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."


# --- AC3: reschedule falls back to the existing no-action summary; a `cancel` plan
# missing an `appointment_token` does too (TICK-036) --------------------------------


def test_reschedule_intent_falls_back_to_the_no_action_summary() -> None:
    booking = FakeBookingService(AssertionError("must not be called"))  # type: ignore[arg-type]
    plan = PlanningOutput(intent="reschedule")

    result = run(tool(booking).execute(plan))

    assert result.public_summary == "No scheduling action is available yet in this demo."
    assert booking.calls == []


def test_cancel_intent_with_no_token_falls_back_to_the_no_action_summary() -> None:
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
