"""Synthetic integration tests for `RescheduleService` (TICK-020): compose a real
`BookingService.book()` and `CancellationService.cancel()` call, book-then-cancel.

Mirrors `test_booking.py`/`test_appointment_cancellation.py`'s own synthetic
(`httpx.MockTransport`) discipline. Per TICK-020's own Testing note, the happy path
and the stale/conflicting-target-slot case are the ones an operator with a running
local Docker stack must additionally prove live
(`evidence/TICK-020/RESCHEDULE_EVIDENCE.md` records what remains manual and why);
cancellation failing after a successful rebook is exercised here only, with a mocked
adapter response, exactly as the ticket's Testing note allows ("harder to force live
on demand").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import Appointment, OpenEmrRequestError
from ai_server.scheduling.appointments import AnonymousAppointmentStore
from ai_server.scheduling.booking import (
    AppointmentRequest,
    BookingService,
    OpenEmrBookingAdapter,
    SlotBookingError,
)
from ai_server.scheduling.cancel import AppointmentCancelAdapter, CancellationService
from ai_server.scheduling.reschedule import (
    RescheduleCancellationFailedError,
    RescheduledAppointment,
    RescheduleService,
)
from ai_server.scheduling.slots import AnonymousSlotStore, CandidateSlot

PORTAL_BASE_URL = "https://openemr.test/apis/default"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)

REQUEST = AppointmentRequest(
    category_id="5", title="Office Visit", facility_id="9", billing_location_id="10"
)


def run(coroutine):
    return asyncio.run(coroutine)


def original_appointment(identifier: str = "old-openemr-id") -> Appointment:
    return Appointment(
        id=identifier,
        status="booked",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=2, minutes=30),
    )


def new_candidate(hours_from_now: float = 48, duration_minutes: int = 30) -> CandidateSlot:
    start = NOW + timedelta(hours=hours_from_now)
    return CandidateSlot(starts_at=start, ends_at=start + timedelta(minutes=duration_minutes))


def service_with(
    book_handler,
    cancel_handler,
) -> tuple[RescheduleService, AnonymousSlotStore, AnonymousAppointmentStore]:
    slot_store = AnonymousSlotStore()
    appointment_store = AnonymousAppointmentStore()
    booking = BookingService(
        slot_store,
        OpenEmrBookingAdapter(
            OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL),
            httpx.AsyncClient(transport=httpx.MockTransport(book_handler)),
        ),
    )
    cancellation = CancellationService(
        appointment_store,
        AppointmentCancelAdapter(
            OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL),
            httpx.AsyncClient(transport=httpx.MockTransport(cancel_handler)),
        ),
    )
    return RescheduleService(booking, cancellation), slot_store, appointment_store


# --- AC2/AC4: happy path books the new slot, then cancels the original -------------


def test_happy_path_books_the_new_slot_then_cancels_the_original() -> None:
    call_order: list[str] = []

    async def book_handler(request: httpx.Request) -> httpx.Response:
        call_order.append("book")
        return httpx.Response(201, json={"id": "new-openemr-id", "status": "booked"})

    async def cancel_handler(request: httpx.Request) -> httpx.Response:
        call_order.append("cancel")
        assert str(request.url) == f"{PORTAL_BASE_URL}/portal/patient/appointment/old-openemr-id"
        return httpx.Response(200, json={"id": "old-openemr-id", "status": "cancelled"})

    service, slot_store, appointment_store = service_with(book_handler, cancel_handler)
    slot_token = slot_store.issue(new_candidate(), NOW).slot_token
    appointment_token = appointment_store.issue(original_appointment(), "patient-a", NOW)

    result = run(
        service.reschedule(
            "token", "patient-a", slot_token, appointment_token.appointment_token, REQUEST, NOW
        )
    )

    assert call_order == ["book", "cancel"]
    assert isinstance(result, RescheduledAppointment)
    assert result.booked.id == "new-openemr-id"
    assert result.cancelled.id == "old-openemr-id"
    assert result.cancelled.status == "cancelled"


# --- AC2: a stale/conflicting target slot fails before cancellation is attempted ---


def test_a_stale_slot_token_fails_before_any_openemr_call_and_the_original_is_untouched() -> None:
    def book_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR booking call is permitted for an unknown slot token")

    def cancel_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("cancellation must never be attempted when booking never happened")

    service, slot_store, appointment_store = service_with(book_handler, cancel_handler)
    appointment_token = appointment_store.issue(original_appointment(), "patient-a", NOW)

    with pytest.raises(SlotBookingError):
        run(
            service.reschedule(
                "token",
                "patient-a",
                "slot_never-issued",
                appointment_token.appointment_token,
                REQUEST,
                NOW,
            )
        )


def test_a_conflicting_target_slot_rejected_by_openemr_leaves_the_original_untouched() -> None:
    """The slot token itself resolves fine, but OpenEMR refuses the booking (e.g. the
    real slot was taken by someone else between discovery and this request) -- the
    original appointment must still never be touched."""

    async def book_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "conflict"})

    def cancel_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("cancellation must never be attempted when booking failed")

    service, slot_store, appointment_store = service_with(book_handler, cancel_handler)
    slot_token = slot_store.issue(new_candidate(), NOW).slot_token
    appointment_token = appointment_store.issue(original_appointment(), "patient-a", NOW)

    with pytest.raises(OpenEmrRequestError):
        run(
            service.reschedule(
                "token", "patient-a", slot_token, appointment_token.appointment_token, REQUEST, NOW
            )
        )


# --- AC3: cancellation failing after a successful rebook is reported, not dropped --


def test_cancellation_failure_after_a_successful_rebook_reports_the_confirmed_booking() -> None:
    async def book_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "new-openemr-id", "status": "booked"})

    async def cancel_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    service, slot_store, appointment_store = service_with(book_handler, cancel_handler)
    slot_token = slot_store.issue(new_candidate(), NOW).slot_token
    appointment_token = appointment_store.issue(original_appointment(), "patient-a", NOW)

    with pytest.raises(RescheduleCancellationFailedError) as excinfo:
        run(
            service.reschedule(
                "token", "patient-a", slot_token, appointment_token.appointment_token, REQUEST, NOW
            )
        )

    assert excinfo.value.booked.id == "new-openemr-id"


def test_cancellation_of_a_stale_appointment_token_after_a_successful_rebook_is_reported() -> None:
    """The appointment_token itself is stale/already-used -- the new appointment
    still booked successfully and that must not be silently lost."""

    async def book_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "new-openemr-id", "status": "booked"})

    def cancel_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR cancel call is permitted for an unknown appointment token")

    service, slot_store, _ = service_with(book_handler, cancel_handler)
    slot_token = slot_store.issue(new_candidate(), NOW).slot_token

    with pytest.raises(RescheduleCancellationFailedError) as excinfo:
        run(service.reschedule("token", "patient-a", slot_token, "appt_never-issued", REQUEST, NOW))

    assert excinfo.value.booked.id == "new-openemr-id"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
