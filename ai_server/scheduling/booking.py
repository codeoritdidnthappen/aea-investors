"""Book a genuinely open slot through the mapped OpenEMR Standard API endpoint.

Booking is deliberately kept out of the read-only `OpenEmrScheduleAdapter`
(`ai_server/openemr/adapter.py`) -- that adapter's own tests assert it exposes no
booking, cancel, or policy method (TICK-018 AC4, "This adapter adds no booking,
eligibility, notice, or scheduling default"). This module owns the one write path
`evidence/TICK-001/ENDPOINT_MATRIX.md` records as "Supported locally": the Standard
API `POST /api/patient/{pid}/appointment`, proven in TICK-001's own probe (`200` with
`{"id": "..."}` for the documented required fields). `OpenEmrBookingAdapter` mirrors
`ai_server/openemr/demographics.py`'s shape exactly, including its most important
property: every method takes the caller's already-delegated bearer token and the
caller's already-established numeric patient id as explicit arguments; this module
never stores, caches, resolves, or infers either one itself.

`BookingService` is the only caller allowed to supply real OpenEMR timing to the
adapter: it always resolves a slot token through `AnonymousSlotStore.resolve()`
(TICK-019) first, so a booking request can never carry a client-supplied start/end
time or OpenEMR identifier (FR-20). Token consumption is single-use and happens before
any OpenEMR request is made, so a double-submitted or concurrently retried booking
request can produce at most one confirmed OpenEMR appointment (AC4): the losing
attempt fails on `resolve()` itself, with no OpenEMR call and no invented commitment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import httpx

from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError
from ai_server.scheduling.slots import AnonymousSlotStore, SlotTokenError

_APPOINTMENT_PATH = "/patient/{patient_id}/appointment"
# list_options 'apptstat' (sql/database.sql): '-' == "- None", OpenEMR's own default
# status for a freshly booked slot with no reminder/check-in history yet. Not a
# cancellation code (contrast `cancel.py`'s status) and not a status this module
# invents -- it is the example value OpenEMR's own Standard API documentation uses for
# this same field.
_BOOKED_STATUS = "-"


class SlotBookingError(Exception):
    """Raised when a slot token cannot be booked; no OpenEMR request was made."""


@dataclass(frozen=True)
class AppointmentRequest:
    """Appointment fields this module never invents a default for.

    OpenEMR's Standard API requires a category, facility, and billing location for
    every booking (`evidence/TICK-001/ENDPOINT_MATRIX.md`); this module has no office
    configuration of its own; the caller (an admin-configured tool wiring, not this
    module) supplies them explicitly, the same discipline
    `ai_server.openemr.demographics.confirm_identity` uses for identity fields.
    """

    category_id: str
    title: str
    facility_id: str
    billing_location_id: str
    provider_id: str | None = None


@dataclass(frozen=True)
class BookedAppointment:
    """The OpenEMR-confirmed result of a booking call; `id` is the real OpenEMR event id."""

    id: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class OpenEmrBookingSettings:
    """Validated configuration for the OpenEMR Standard API booking boundary."""

    api_base_url: str

    @classmethod
    def from_environment(cls) -> OpenEmrBookingSettings:
        """Parse the required Standard API base URL once during application startup."""
        base_url = os.environ.get("OPENEMR_API_BASE_URL")
        if not base_url:
            raise OpenEmrConfigurationError("OPENEMR_API_BASE_URL is required")
        return cls(api_base_url=base_url.rstrip("/"))


class OpenEmrBookingAdapter:
    """Creates one appointment through the mapped Standard API route only.

    Every method takes the caller's already-delegated bearer token and the caller's
    already-established numeric patient id; like `OpenEmrDemographicsAdapter`, this
    adapter never stores, caches, or otherwise retains either one, and never resolves
    "the logged-in patient" itself.
    """

    def __init__(self, settings: OpenEmrBookingSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def create_appointment(
        self,
        access_token: str,
        patient_id: str,
        *,
        starts_at: datetime,
        ends_at: datetime,
        request: AppointmentRequest,
    ) -> str:
        """POST the required Standard API fields; return only OpenEMR's new event id.

        Raises `OpenEmrRequestError` for any non-200 response or an unusable body --
        never returns a fabricated id (AC3).
        """
        body: dict[str, object] = {
            "pc_catid": request.category_id,
            "pc_title": request.title,
            "pc_duration": int((ends_at - starts_at).total_seconds()),
            "pc_hometext": "Booked by the AI scheduling assistant",
            "pc_apptstatus": _BOOKED_STATUS,
            "pc_eventDate": starts_at.date().isoformat(),
            "pc_startTime": starts_at.strftime("%H:%M"),
            "pc_facility": request.facility_id,
            "pc_billing_location": request.billing_location_id,
        }
        if request.provider_id is not None:
            body["pc_aid"] = request.provider_id
        path = _APPOINTMENT_PATH.format(patient_id=patient_id)
        try:
            response = await self._client.post(
                f"{self._settings.api_base_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError("booking the appointment in OpenEMR failed") from exc
        if response.status_code != 200:
            raise OpenEmrRequestError(
                f"OpenEMR booking request failed with status {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenEmrRequestError("OpenEMR returned an invalid booking response") from exc
        identifier = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            raise OpenEmrRequestError("OpenEMR returned an invalid booking response")
        return str(identifier)


class BookingService:
    """Resolve a slot token to its real window, then book it through OpenEMR.

    This is the only place a slot token is ever turned into an OpenEMR write: the
    token store enforces single-use, atomic-in-process consumption
    (`AnonymousSlotStore.resolve`, TICK-019), so of any number of concurrent or
    double-submitted `book()` calls sharing the same token, at most one ever reaches
    `OpenEmrBookingAdapter.create_appointment` (AC4).
    """

    def __init__(self, store: AnonymousSlotStore, adapter: OpenEmrBookingAdapter) -> None:
        self._store = store
        self._adapter = adapter

    async def book(
        self,
        access_token: str,
        patient_id: str,
        slot_token: str,
        request: AppointmentRequest,
        now: datetime,
    ) -> BookedAppointment:
        """Book the genuine slot behind `slot_token`, or fail with no OpenEMR write.

        Raises `SlotBookingError` for an unknown, already-used, or expired token
        (stale-slot conflict, AC3) -- OpenEMR is never called in that case. Raises
        `OpenEmrRequestError` if OpenEMR itself refuses or fails the booking. Either
        way, a caller only ever receives a `BookedAppointment` once OpenEMR has
        actually confirmed one (AC3): there is no code path here that returns success
        without a real OpenEMR response backing it.
        """
        try:
            candidate = self._store.resolve(slot_token, now)
        except SlotTokenError as exc:
            raise SlotBookingError(str(exc)) from exc
        identifier = await self._adapter.create_appointment(
            access_token,
            patient_id,
            starts_at=candidate.starts_at,
            ends_at=candidate.ends_at,
            request=request,
        )
        return BookedAppointment(
            id=identifier, starts_at=candidate.starts_at, ends_at=candidate.ends_at
        )
