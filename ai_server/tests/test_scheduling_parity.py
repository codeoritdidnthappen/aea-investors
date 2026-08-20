"""Native/chat parity matrix for scheduling actions (TICK-021).

FR-28: appointment actions use the booking, cancellation, notice, and eligibility
rules OpenEMR itself enforces; the AI server -- the boundary a chat scheduling tool
calls -- defines no separate policy of its own (`ai_server/scheduling/booking.py`,
`ai_server/scheduling/cancel.py`). Each case below pairs the OpenEMR-authoritative
outcome for one synthetic scenario with the outcome `ai_server.scheduling` actually
produces for that same scenario and asserts they match exactly: the "native" side is
the documented/mocked OpenEMR response
(`evidence/TICK-001/ENDPOINT_MATRIX.md`,
`openemr_modules/aeai-portal-chat/src/Service/AppointmentCancelService.php`), and the
"chat" side is what `ai_server.scheduling` returns or raises when OpenEMR responds
that way. A discrepancy here is a real policy drift, not a flaky test, and fails CI
(AC3). `evidence/TICK-021/PARITY_MATRIX.md` is the human-readable table this file
backs; keep the two in sync.

Reschedule has no row here: no OpenEMR route or callable service method exists to
compare against (`evidence/TICK-001/ENDPOINT_MATRIX.md`'s reschedule row; TICK-020's
`blocked_reason`), so it is documented as not exposed/not applicable rather than
verified, per this ticket's dependency note.
`test_reschedule_is_documented_as_not_exposed_not_applicable` locks that
documentation in rather than silently drifting if either file changes.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.scheduling import booking as booking_module
from ai_server.scheduling import cancel as cancel_module
from ai_server.scheduling.booking import (
    AppointmentRequest,
    BookedAppointment,
    BookingService,
    OpenEmrBookingAdapter,
    OpenEmrBookingSettings,
)
from ai_server.scheduling.cancel import (
    AppointmentAlreadyCancelledError,
    AppointmentCancelAdapter,
    AppointmentNotFoundError,
    CancelledAppointment,
)
from ai_server.scheduling.slots import AnonymousSlotStore, CandidateSlot

_REPO_ROOT = Path(__file__).resolve().parents[2]
API_BASE_URL = "https://openemr.test/apis/default/api"
PORTAL_BASE_URL = "https://openemr.test/apis/default"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)

REQUEST = AppointmentRequest(
    category_id="5", title="Office Visit", facility_id="9", billing_location_id="10"
)


def run(coroutine):
    return asyncio.run(coroutine)


def booking_service(handler: httpx.MockTransport, store: AnonymousSlotStore | None = None):
    active_store = store or AnonymousSlotStore()
    client = httpx.AsyncClient(transport=handler)
    adapter = OpenEmrBookingAdapter(OpenEmrBookingSettings(api_base_url=API_BASE_URL), client)
    return BookingService(active_store, adapter), active_store


def cancel_adapter(handler: httpx.MockTransport) -> AppointmentCancelAdapter:
    client = httpx.AsyncClient(transport=handler)
    return AppointmentCancelAdapter(OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL), client)


def candidate(hours_from_now: float, duration_minutes: int = 30) -> CandidateSlot:
    start = NOW + timedelta(hours=hours_from_now)
    return CandidateSlot(starts_at=start, ends_at=start + timedelta(minutes=duration_minutes))


# --- BOOKING ------------------------------------------------------------------------


def test_book_allowed_native_confirmation_and_chat_result_match() -> None:
    """Native: OpenEMR 200 {"id": "501"} for a valid open slot (ENDPOINT_MATRIX.md,
    "Create appointment / book"). Chat: `BookingService.book` must surface the same
    id and the same OpenEMR-resolved window, never a client-supplied or invented one.
    """

    async def native(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "501"})

    store = AnonymousSlotStore()
    slot = candidate(hours_from_now=48)
    issued = store.issue(slot, NOW)
    service, _ = booking_service(httpx.MockTransport(native), store)

    chat_result = run(service.book("token", "7", issued.slot_token, REQUEST, NOW))

    native_result = BookedAppointment(id="501", starts_at=slot.starts_at, ends_at=slot.ends_at)
    assert chat_result == native_result


def test_book_denied_conflict_native_rejection_and_chat_rejection_match() -> None:
    """Native: OpenEMR returns a non-200 (e.g. 409) when the slot is no longer open
    (TICK-031 AC3, "conflict or stale-slot ... responses are clear and create no
    invented commitment"). Chat: the same request must fail explicitly, with no
    `BookedAppointment` ever produced.
    """

    async def native(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "slot no longer open"})

    store = AnonymousSlotStore()
    issued = store.issue(candidate(hours_from_now=48), NOW)
    service, _ = booking_service(httpx.MockTransport(native), store)

    with pytest.raises(OpenEmrRequestError):
        run(service.book("token", "7", issued.slot_token, REQUEST, NOW))


def test_book_double_submit_native_receives_one_create_call_nfr11() -> None:
    """NFR-11: double-submitted/concurrent booking attempts create no more than one
    confirmed appointment. Native only ever sees one `POST .../appointment` call
    (the losing chat attempt fails on the token store before reaching OpenEMR at
    all), and chat surfaces exactly one confirmed booking to match.
    """
    native_calls: list[httpx.Request] = []

    async def native(request: httpx.Request) -> httpx.Response:
        native_calls.append(request)
        return httpx.Response(200, json={"id": "77"})

    store = AnonymousSlotStore()
    issued = store.issue(candidate(hours_from_now=48), NOW)
    service, _ = booking_service(httpx.MockTransport(native), store)

    async def scenario() -> list[object]:
        return await asyncio.gather(
            service.book("token", "7", issued.slot_token, REQUEST, NOW),
            service.book("token", "7", issued.slot_token, REQUEST, NOW),
            return_exceptions=True,
        )

    results = run(scenario())

    confirmed = [r for r in results if isinstance(r, BookedAppointment)]
    assert len(native_calls) == 1
    assert len(confirmed) == 1


def test_book_notice_no_independent_minimum_enforced_beyond_openemr() -> None:
    """Notice (FR-28): `evidence/TICK-001/ENDPOINT_MATRIX.md`'s booking row documents
    no minimum-notice validation on OpenEMR's own endpoint -- only the listed content
    fields. `scheduling_rules.minimum_booking_notice_minutes`
    (`ai_server/app/chat.py`) is prompt context for the model, never a local gate. A
    near-term slot (5 minutes out) must therefore be booked identically to a
    far-future one: chat invents no stricter policy than OpenEMR itself enforces.
    """

    async def native(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "9001"})

    store = AnonymousSlotStore()
    near_term = candidate(hours_from_now=5 / 60)
    issued = store.issue(near_term, NOW)
    service, _ = booking_service(httpx.MockTransport(native), store)

    chat_result = run(service.book("token", "7", issued.slot_token, REQUEST, NOW))

    assert chat_result == BookedAppointment(
        id="9001", starts_at=near_term.starts_at, ends_at=near_term.ends_at
    )


def test_book_and_slot_source_never_reference_a_local_notice_threshold() -> None:
    """Static counterpart to the case above: no booking/slot code path reads
    `minimum_booking_notice_minutes` or otherwise names a local notice policy --
    only OpenEMR's own acceptance or rejection governs (FR-28).
    """
    for module in (booking_module, __import__("ai_server.scheduling.slots", fromlist=["_"])):
        source = inspect.getsource(module)
        assert "minimum_booking_notice" not in source
        assert "notice_minutes" not in source


# --- CANCELLATION ---------------------------------------------------------------


def test_cancel_allowed_native_confirmation_and_chat_result_match() -> None:
    """Native: OpenEMR's own cancel-by-status route returns 200 with the id and the
    real cancelled-with-history status (`AppointmentCancelService.php`,
    `CANCELLED_STATUS = 'x'`). Chat must surface the identical result.
    """

    async def native(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "appt-1", "status": "cancelled"})

    chat_result = run(cancel_adapter(httpx.MockTransport(native)).cancel("token", "appt-1"))

    assert chat_result == CancelledAppointment(id="appt-1", status="cancelled")


def test_cancel_eligibility_denied_for_another_patients_appointment() -> None:
    """Eligibility (FR-28): the only appointment a patient is eligible to cancel is
    their own. `AppointmentCancelService::forPatient` filters by both the
    token-derived `puuid` and the requested `auuid` in a single `search()` call
    (`test_appointment_cancel_module.py`), so a foreign or unknown appointment id
    404s -- it is never found, not found-then-rejected. Chat must raise the same
    "not eligible" outcome, never a success.
    """

    async def native(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no appointment with that id for this patient"})

    with pytest.raises(AppointmentNotFoundError):
        run(cancel_adapter(httpx.MockTransport(native)).cancel("token", "not-mine"))


def test_cancel_already_cancelled_native_message_surfaces_unaltered() -> None:
    """AC3: a discrepancy must identify the authoritative OpenEMR response. Chat must
    raise with OpenEMR's own message text, not a locally invented one.
    """
    native_message = "this appointment is already cancelled"

    async def native(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": native_message})

    with pytest.raises(AppointmentAlreadyCancelledError, match=native_message):
        run(cancel_adapter(httpx.MockTransport(native)).cancel("token", "appt-1"))


# --- RESCHEDULE: not exposed, documented rather than verified -----------------------


def test_reschedule_has_no_callable_path_in_ai_server_scheduling() -> None:
    """Locks in the absence this matrix documents: neither scheduling module exposes
    a reschedule operation to relay, because none exists to relay
    (`evidence/TICK-001/ENDPOINT_MATRIX.md`).
    """
    for module in (booking_module, cancel_module):
        public_names = {name for name in dir(module) if not name.startswith("_")}
        assert not any("reschedule" in name.lower() for name in public_names)


def test_reschedule_is_documented_as_not_exposed_not_applicable() -> None:
    """TICK-020 (reschedule) is not yet built: re-scoped 2026-08-20 to a
    cancel-then-rebook plan that explicitly depends on TICK-039 landing first
    (`depends_on` in its own ticket file), because no OpenEMR service method for an
    in-place reschedule exists on this pinned release either way. This ticket's own
    AC says reschedule must be documented as not exposed/not applicable rather than
    verified until then. Checking the committed files directly (not a hand-copied
    assumption) keeps this test honest if either one is ever edited.
    """
    tick_020 = (_REPO_ROOT / "tickets" / "TICK-020-manage-appointments.md").read_text(
        encoding="utf-8"
    )
    assert "status: todo" in tick_020
    assert "TICK-039" in tick_020
    assert "no OpenEMR service method exists" in tick_020 or "no update method" in tick_020

    matrix = (_REPO_ROOT / "evidence" / "TICK-021" / "PARITY_MATRIX.md").read_text(encoding="utf-8")
    assert "Reschedule" in matrix
    assert "not exposed" in matrix.lower() or "not applicable" in matrix.lower()
