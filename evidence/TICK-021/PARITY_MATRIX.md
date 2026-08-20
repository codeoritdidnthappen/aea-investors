# TICK-021 — native/chat scheduling policy parity matrix

FR-28: appointment actions use the booking, rescheduling, cancellation, notice, and
eligibility rules already enforced by OpenEMR; the AI server defines no separate
scheduling policy or default. `ai_server/scheduling/booking.py` and
`ai_server/scheduling/cancel.py` (TICK-031) are the boundary a chat scheduling tool
calls once wired — this matrix proves that boundary relays OpenEMR's own decision
unchanged for every case OpenEMR exposes, and documents the one case it doesn't
(reschedule) as not exposed rather than verified, per this ticket's dependency note.

Every row is backed by an automated test in
`ai_server/tests/test_scheduling_parity.py` using synthetic data (AC2): the "native"
column is the documented or mocked OpenEMR-authoritative response; the "chat" column
is what `ai_server.scheduling` actually returns or raises for that same response. A
mismatch fails the named test, which fails CI, which blocks release (AC3) — that is
the enforcement mechanism this ticket adds, not a manual sign-off.

## Booking

| Case | Native (OpenEMR-authoritative) result | Chat (`ai_server.scheduling`) result | Verdict | Test |
|---|---|---|---|---|
| Allowed: valid open slot, required fields present | `200 {"id": "501"}` (`evidence/TICK-001/ENDPOINT_MATRIX.md`, "Create appointment / book") | `BookedAppointment(id="501", starts_at=<resolved>, ends_at=<resolved>)` | PARITY | `test_book_allowed_native_confirmation_and_chat_result_match` |
| Denied: slot conflict / no longer open | Non-`200` (e.g. `409`) | `OpenEmrRequestError` raised; no `BookedAppointment` ever produced | PARITY (both refuse) | `test_book_denied_conflict_native_rejection_and_chat_rejection_match` |
| Double-submitted / concurrent (NFR-11) | Exactly one `POST .../appointment` call ever reaches OpenEMR | Exactly one `BookedAppointment` confirmed; the losing attempt fails on the token store before any OpenEMR call | PARITY | `test_book_double_submit_native_receives_one_create_call_nfr11` |
| Notice: near-term slot (5 minutes out) | No minimum-notice field or rejection documented on this endpoint (`ENDPOINT_MATRIX.md` lists only `pc_catid`/`pc_title`/`pc_duration`/`pc_hometext`/`pc_apptstatus`/`pc_eventDate`/`pc_startTime`/`pc_facility`/`pc_billing_location`/`pc_aid`) — accepted the same as a far-future slot | Booked identically; `minimum_booking_notice_minutes` (`ai_server/app/chat.py`, `ai_server/privacy/gate.py`) is prompt context only, never a local gate | PARITY (neither enforces a minimum) | `test_book_notice_no_independent_minimum_enforced_beyond_openemr`, `test_book_and_slot_source_never_reference_a_local_notice_threshold` |

## Cancellation and eligibility

| Case | Native (OpenEMR-authoritative) result | Chat (`ai_server.scheduling`) result | Verdict | Test |
|---|---|---|---|---|
| Allowed: patient cancels their own appointment | `200 {"id": ..., "status": "cancelled"}` via `AppointmentService::updateAppointmentStatus`, `CANCELLED_STATUS = 'x'` (retains history, never `DELETE`) | `CancelledAppointment(id=..., status="cancelled")`, identical | PARITY | `test_cancel_allowed_native_confirmation_and_chat_result_match` |
| Eligibility denied: another patient's or an unknown appointment id | `404` — `AppointmentCancelService::forPatient` filters by both the token-derived `puuid` and the requested `auuid` in one `search()` call, so a mismatch is never found, not found-then-rejected (`openemr_modules/aeai-portal-chat/.../AppointmentCancelService.php`, locked by `ai_server/tests/test_appointment_cancel_module.py`) | `AppointmentNotFoundError` raised; no cancellation ever implied | PARITY | `test_cancel_eligibility_denied_for_another_patients_appointment` |
| Already cancelled | `409 {"error": "this appointment is already cancelled"}` | `AppointmentAlreadyCancelledError` raised carrying OpenEMR's own message text unaltered (AC3: "identifies the authoritative OpenEMR response") | PARITY | `test_cancel_already_cancelled_native_message_surfaces_unaltered` |

## Reschedule — composed, not a native OpenEMR capability

| Case | Result |
|---|---|
| Reschedule an existing appointment's date/time/duration | **Still not exposed as a single OpenEMR capability on v8.3.0.** No Standard/FHIR route and no callable `AppointmentService` update method exist — only ~150 lines of inline SQL in the legacy `interface/main/calendar/add_edit_event.php` page (`evidence/TICK-001/ENDPOINT_MATRIX.md`'s reschedule row). TICK-020 (`tickets/TICK-020-manage-appointments.md`), re-scoped 2026-08-20, does not add a third OpenEMR write path for this: `ai_server.scheduling.reschedule.RescheduleService` instead **composes** the booking and cancellation rows already proven above — book the new slot first, and only cancel the original once OpenEMR has confirmed the new one — so there is still no single native reschedule policy to parity-check, only the two policies this matrix already covers, called in sequence. |

Locked by `test_reschedule_has_no_native_write_path_of_its_own_in_booking_or_cancel_modules`
(no reschedule-named symbol exists in `ai_server.scheduling.booking`/`cancel` — the
composition lives in its own `ai_server.scheduling.reschedule` module) and
`test_reschedule_is_documented_as_a_composition_not_a_native_openemr_capability` (this
file and TICK-020's own file stay in sync with that claim). `ai_server/tests/
test_reschedule.py` is `RescheduleService`'s own synthetic coverage: the happy path
(book then cancel, in that order), a stale/conflicting target slot (booking fails,
original never touched), and cancellation failing after a successful rebook (reported
via `RescheduleCancellationFailedError`, never silently dropped). Live proof status
for the happy path and the conflicting-slot case is tracked separately in
`evidence/TICK-020/RESCHEDULE_EVIDENCE.md`, mirroring this file's own "What this does
not cover, and why" section below.

## What this does not cover, and why

`ai_server/tests/test_scheduling_parity.py` is fully synthetic (mocked OpenEMR HTTP
responses) and runs in CI with no external dependency, satisfying AC2's "using
synthetic data" requirement and giving AC3 a real enforcement mechanism (a failing
assertion blocks the merge gate).

The ticket's Testing note also asks for "remaining browser/native checks against the
local stack" recorded as evidence, the same discipline
`evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md` and
`evidence/TICK-028/BINDING_MATRIX.md` used. That was not performed as part of this
change: it requires a running local OpenEMR/MariaDB stack reachable over HTTPS, which
this build environment does not have running, and bringing one up is outside a build
worker's permitted actions (no long-running server processes). `scripts/
probe_scheduling_parity.py` reproduces the cancellation half of this matrix
(own-cancel, cross-patient-denied, already-cancelled) against a live stack — an
operator with a running `deploy/local` stack and two synthetic patients (one with an
existing active appointment) can run it and append the results here.

A live booking probe is not included: it requires resolving OpenEMR's *internal
numeric* patient id (`pid`) from a patient-context token to call `POST
/api/patient/{pid}/appointment`, and no adapter in this codebase resolves that id
today (`BookingService`/`OpenEmrBookingAdapter` take it as an already-established
argument supplied by "an admin-configured tool wiring, not this module" —
`ai_server/scheduling/booking.py`). That resolution mechanism does not exist yet;
recording it here rather than guessing at one keeps this evidence honest. Until it
does, `test_book_allowed_native_confirmation_and_chat_result_match` (synthetic) is
the strongest available proof for the booking-allowed case.
