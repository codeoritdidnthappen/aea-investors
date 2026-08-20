"""Compose booking and cancellation into a reschedule: move an existing appointment
to a new open slot.

OpenEMR v8.3.0's `AppointmentService` has no method to update an existing
appointment's date/time/duration -- only ~150 lines of inline SQL in the legacy
`interface/main/calendar/add_edit_event.php` page, which `evidence/TICK-001/
ENDPOINT_MATRIX.md` and ADR-3/ADR-4 (`ARCHITECTURE.md`) rule out as a workaround
(TICK-020's own "Scope narrowed" note). What OpenEMR does expose, and what this
module composes instead, is the same two real, callable operations `BookingService`
(TICK-031/TICK-034) and `CancellationService` (TICK-031/TICK-036) already use: book
the new slot first, then cancel the original appointment (TICK-020 AC1) -- no new raw
SQL, no new OpenEMR write path, and no dependency on the separately-selected chat
`cancel` intent (TICK-039 sidestepped by construction, not solved).

This is not one atomic OpenEMR transaction -- the two calls are separate REST
requests, not a database transaction -- so this module's whole job is making the
partial-failure case (new slot booked, original cancellation then fails) an honest,
explicit outcome rather than a silently lossy one (TICK-020 AC3).

`RescheduleService` never invents a fallback OpenEMR write path of its own: booking
resolves through `AnonymousSlotStore` (TICK-019) via `BookingService`, exactly the
same as a plain `book` plan, and cancellation resolves through
`AnonymousAppointmentStore` (TICK-036) via `CancellationService`, exactly the same as
a plain `cancel` plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.scheduling.booking import AppointmentRequest, BookedAppointment, BookingService
from ai_server.scheduling.cancel import (
    AppointmentAlreadyCancelledError,
    AppointmentNotFoundError,
    CancellationService,
    CancelledAppointment,
    StaleAppointmentTokenError,
)


@dataclass(frozen=True)
class RescheduledAppointment:
    """The OpenEMR-confirmed result of a full reschedule: both calls succeeded."""

    booked: BookedAppointment
    cancelled: CancelledAppointment


class RescheduleCancellationFailedError(Exception):
    """Raised when the new appointment was booked and confirmed by OpenEMR, but
    cancelling the original appointment then failed for any reason (a stale or
    already-used appointment token, an already-cancelled appointment, an appointment
    OpenEMR could not find for this patient, or an OpenEMR request failure).

    Carries the OpenEMR-confirmed `booked` appointment so a caller can tell the
    patient plainly that the new appointment is confirmed while the original one may
    still be active (TICK-020 AC3) -- this state is never silently dropped or papered
    over.
    """

    def __init__(self, message: str, booked: BookedAppointment) -> None:
        super().__init__(message)
        self.booked = booked


class RescheduleService:
    """Book a new slot, then cancel the original appointment, in that order.

    The order is the whole safety property this class exists to enforce (TICK-020
    AC2): booking happens first and must be OpenEMR-confirmed before the original
    appointment is ever touched, so a stale or already-taken target slot never leaves
    the patient with both appointments cancelled and nothing rebooked. Whatever
    `BookingService.book` raises (`SlotBookingError` for a stale/already-used slot
    token, `OpenEmrRequestError` for an OpenEMR refusal) propagates unchanged, and
    cancellation is never attempted in that case.
    """

    def __init__(self, booking: BookingService, cancellation: CancellationService) -> None:
        self._booking = booking
        self._cancellation = cancellation

    async def reschedule(
        self,
        access_token: str,
        patient_id: str,
        slot_token: str,
        appointment_token: str,
        request: AppointmentRequest,
        now: datetime,
    ) -> RescheduledAppointment:
        """Reschedule to the slot behind `slot_token`, cancelling the appointment
        behind `appointment_token`.

        Raises whatever `BookingService.book` raises with no cancellation ever
        attempted if the new slot cannot be booked (AC2). Raises
        `RescheduleCancellationFailedError` if the new appointment booked
        successfully but the original could not then be cancelled (AC3) -- the
        caller must surface both outcomes, not just the exception type. A caller
        only ever receives a `RescheduledAppointment` once both real OpenEMR calls
        have actually returned success (AC4).
        """
        booked = await self._booking.book(access_token, patient_id, slot_token, request, now)
        try:
            cancelled = await self._cancellation.cancel(
                access_token, patient_id, appointment_token, now
            )
        except (
            StaleAppointmentTokenError,
            AppointmentNotFoundError,
            AppointmentAlreadyCancelledError,
            OpenEmrRequestError,
        ) as exc:
            raise RescheduleCancellationFailedError(str(exc), booked) from exc
        return RescheduledAppointment(booked=booked, cancelled=cancelled)
