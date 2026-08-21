"""Book a genuinely open slot through the module-added Portal API endpoint.

Booking is deliberately kept out of the read-only `OpenEmrScheduleAdapter`
(`ai_server/openemr/adapter.py`) -- that adapter's own tests assert it exposes no
booking, cancel, or policy method (TICK-018 AC4, "This adapter adds no booking,
eligibility, notice, or scheduling default"). This module owns the one write path.

TICK-040: the Standard API route this originally called
(`POST /api/patient/{pid}/appointment`, TICK-001's own probe) is gated by a staff
ACL check (`RestConfig::request_authorization_check()` -> `AclMain::aclCheckCore()`
against a logged-in staff `authUser`), never an OAuth scope -- structurally
unreachable for a genuine patient-context bearer token (confirmed directly in the
pinned image's own source; see `tickets/TICK-040-add-portal-booking-route.md`). This
now calls `AppointmentBookController`'s module-added Portal route instead
(`openemr_modules/aeai-portal-chat`), the same `RestApiExtend` mechanism
`AppointmentCancelController` (TICK-036/041) already uses successfully -- enforced by
`AuthorizationListener`'s OAuth-scope check, which a patient token can actually
satisfy. That route resolves the caller's numeric OpenEMR patient id itself, from the
bearer token server-side, so this adapter no longer takes or sends one.

`OpenEmrBookingAdapter` mirrors `ai_server/openemr/demographics.py`'s shape
otherwise: every method takes the caller's already-delegated bearer token as an
explicit argument; this module never stores, caches, resolves, or infers it itself.

`BookingService` is the only caller allowed to supply real OpenEMR timing to the
adapter: it always resolves a slot token through `AnonymousSlotStore.resolve()`
(TICK-019) first, so a booking request can never carry a client-supplied start/end
time or OpenEMR identifier (FR-20). Token consumption is single-use and happens before
any OpenEMR request is made, so a double-submitted or concurrently retried booking
request can produce at most one confirmed OpenEMR appointment (AC4): the losing
attempt fails on `resolve()` itself, with no OpenEMR call and no invented commitment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.scheduling.slots import AnonymousSlotStore, SlotTokenError

_APPOINTMENT_PATH = "/portal/patient/appointment"


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


class OpenEmrBookingAdapter:
    """Creates one appointment through the module-added Portal route only.

    Every method takes the caller's already-delegated bearer token; like
    `OpenEmrDemographicsAdapter`, this adapter never stores, caches, or otherwise
    retains it, and never resolves "the logged-in patient" itself -- the Portal route
    does that server-side, from the token, so this adapter no longer takes or sends a
    patient id at all (TICK-040).
    """

    def __init__(self, settings: OpenEmrPortalSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def create_appointment(
        self,
        access_token: str,
        *,
        starts_at: datetime,
        ends_at: datetime,
        request: AppointmentRequest,
    ) -> str:
        """POST the required fields; return only OpenEMR's new appointment id.

        Raises `OpenEmrRequestError` for any non-201 response or an unusable body --
        never returns a fabricated id (AC3).
        """
        body: dict[str, object] = {
            "pc_catid": request.category_id,
            "pc_title": request.title,
            "pc_duration": int((ends_at - starts_at).total_seconds()),
            "pc_eventDate": starts_at.date().isoformat(),
            "pc_startTime": starts_at.strftime("%H:%M"),
            "pc_facility": request.facility_id,
            "pc_billing_location": request.billing_location_id,
        }
        if request.provider_id is not None:
            body["pc_aid"] = request.provider_id
        try:
            response = await self._client.post(
                f"{self._settings.portal_base_url}{_APPOINTMENT_PATH}",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError("booking the appointment in OpenEMR failed") from exc
        if response.status_code != 201:
            raise OpenEmrRequestError(
                f"OpenEMR booking request failed with status {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenEmrRequestError("OpenEMR returned an invalid booking response") from exc
        identifier = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(identifier, bool) or not isinstance(identifier, str) or not identifier:
            raise OpenEmrRequestError("OpenEMR returned an invalid booking response")
        return identifier


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
            starts_at=candidate.starts_at,
            ends_at=candidate.ends_at,
            request=request,
        )
        return BookedAppointment(
            id=identifier, starts_at=candidate.starts_at, ends_at=candidate.ends_at
        )
