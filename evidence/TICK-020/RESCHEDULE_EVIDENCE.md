# TICK-020 — reschedule (cancel-then-rebook) evidence

FR-13/FR-20/FR-28/NFR-11: a patient can move an existing appointment to a genuinely
open slot, without OpenEMR ever confirming both a booked new appointment and a
cancelled old one being papered over as a single silent success. TICK-020's own
"Re-scoped" note explains why this is a *composition* of `BookingService` (TICK-031/
TICK-034) and `CancellationService` (TICK-031/TICK-036) — `ai_server.scheduling
.reschedule.RescheduleService` — rather than a new OpenEMR write path: OpenEMR
v8.3.0 has no callable service method for an in-place reschedule
(`evidence/TICK-001/ENDPOINT_MATRIX.md`'s reschedule row).

## What is proven, and how

Fully synthetic (mocked OpenEMR HTTP responses via `httpx.MockTransport`), the same
discipline `ai_server/tests/test_booking.py` and `ai_server/tests/
test_appointment_cancellation.py` already use for the two services this one composes:

- `ai_server/tests/test_reschedule.py`
  - **Happy path** (AC2, AC4): the new slot is booked first, and only cancelled
    second, in that order (asserted call order, not just final state) --
    `test_happy_path_books_the_new_slot_then_cancels_the_original`.
  - **Stale/conflicting target slot** (AC2): an unknown/already-used slot token
    fails before any OpenEMR call, and a slot OpenEMR itself rejects (e.g. taken
    between discovery and this request, `409`) fails with no cancellation ever
    attempted either way -- `test_a_stale_slot_token_fails_before_any_openemr_call_
    and_the_original_is_untouched`, `test_a_conflicting_target_slot_rejected_by_
    openemr_leaves_the_original_untouched`.
  - **Cancellation failing after a successful rebook** (AC3, the ticket's own
    Testing note allows a mocked adapter response for exactly this edge case):
    `test_cancellation_failure_after_a_successful_rebook_reports_the_confirmed_
    booking`, `test_cancellation_of_a_stale_appointment_token_after_a_successful_
    rebook_is_reported` -- both assert `RescheduleCancellationFailedError` carries
    the OpenEMR-confirmed new appointment, so it can never be silently dropped.
- `ai_server/tests/test_booking_tool.py` exercises the same four outcomes (success,
  stale slot, booking failure, cancellation-after-rebook failure, plus every
  missing-token/missing-credential fallback) through `BookingTool._execute_reschedule`,
  asserting the exact `public_summary` text a patient would see for each (AC3, AC4:
  "may still be active -- please contact the clinic").
- `ai_server/tests/test_scheduling_tool_wiring.py` proves `_build_scheduling_tool`
  wires one `RescheduleService` per turn that composes the *same*
  `BookingService`/`CancellationService` instances `book`/`cancel` intents already
  use (not fresh ones), so the single-use slot/appointment token guarantees those
  services already provide still hold for a reschedule.
- `ai_server/tests/test_scheduling_parity.py` and `evidence/TICK-021/
  PARITY_MATRIX.md` are updated to document reschedule as a composition of the two
  policy rows already parity-checked there, rather than the previous "not exposed/
  not applicable" state (true before TICK-034/TICK-036 shipped booking/cancellation
  as real, callable operations).

## What is not proven live, and why

TICK-020's own Testing note asks for the happy path and the stale/conflicting-slot
case to be "proven live, with real OpenEMR responses" against the local Docker
stack. That was not performed as part of this change, for two separate reasons:

1. **Bringing up a local OpenEMR/MariaDB/AI-server stack is outside a build
   worker's permitted actions** (no long-running server processes) -- the identical
   limitation `scripts/probe_scheduling_parity.py`'s own docstring and
   `evidence/TICK-021/PARITY_MATRIX.md`'s "What this does not cover, and why"
   section already record for TICK-021, and that TICK-031/TICK-034/TICK-036 shipped
   under (none of their own evidence directories contain a live-Docker capture
   either).
2. **The booking half specifically inherits a real, pre-existing platform gap**,
   not one this ticket introduces: `evidence/TICK-001/ENDPOINT_MATRIX.md`'s own
   "Create appointment / book" row records booking as "**Supported locally**.
   Production patient-scoped authorization remains unproven" -- and
   `evidence/TICK-021/PARITY_MATRIX.md` separately records that no live booking
   probe exists at all, because nothing in this codebase resolves whether a genuine
   patient OAuth token actually carries a scope OpenEMR's booking route accepts
   (`user/appointment.cruds` per `ENDPOINT_MATRIX.md`'s "Create appointment / book"
   row; `AuthSettings.scopes`, `ai_server/app/auth.py`, grants only
   `patient/appointment.u`, the module-added cancel-by-status route, never a
   booking-create scope -- and TICK-033 rules out registering any `user/*` scope for
   this client at all, because it forces a staff-style consent screen a genuine
   patient must never see). This is the same reason `_SCHEDULING_RULES.booking_enabled`
   stayed `False` through TICK-034/TICK-036/TICK-039 -- a real, load-bearing
   constraint, not merely an unrelated demo choice. An operator attempting a live
   reschedule probe today would hit this exact gap on the booking call, the same as
   a live booking-only probe would.

Closing gap 2 (granting the patient client a scope that lets `POST /api/patient/
{pid}/appointment` succeed for a real patient token, without triggering the
`user/*` consent-screen problem TICK-033 already ruled out) is real, separately
scoped work this ticket does not attempt -- recording it here rather than shipping
an untested, likely-failing probe script keeps this evidence honest, the same
discipline `evidence/TICK-021/PARITY_MATRIX.md` already used for the identical
booking-probe gap. Once that scope question is resolved, an operator can extend
`scripts/probe_scheduling_parity.py`'s already-proven login/API helpers with a
`POST /api/patient/{uuid}/appointment` call followed by the existing
`PUT /portal/patient/appointment/{id}` cancel call to complete this live proof.

`rescheduling_enabled=True` (`ai_server/app/chat.py`) reflects that the composed
capability itself is real and wired, mirroring `cancellation_enabled=True`
(TICK-036) rather than `booking_enabled=False` (TICK-039's deliberate, unrelated
demo scoping) -- AC1 requires that a patient *can* reschedule through the dedicated
plan intent, which this enables, even though (like plain booking) `open_slots` stays
empty in production today absent a mapped candidate-slot source (ADR-3,
`ARCHITECTURE.md`) -- a pre-existing, permanent gap this ticket does not attempt to
close either.
