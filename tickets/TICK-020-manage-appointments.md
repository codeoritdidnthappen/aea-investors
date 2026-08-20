---
id: TICK-020
title: "feat(scheduling): reschedule an appointment through OpenEMR"
type: feature
epic: EPIC-07
priority: P1
estimate: L
depends_on: [TICK-018, TICK-019, TICK-034, TICK-036, TICK-039]
labels: [scheduling, openemr]
source: [FR-13, FR-20, FR-28, NFR-11]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/21
---
## Context

Appointment writes occur only after a deterministic OpenEMR call; the assistant may not claim success before that response.

`depends_on` still includes TICK-019: moving an existing appointment to a new
time needs TICK-019's anonymous-slot discovery to find a genuinely open
target slot, the same as booking does, not just TICK-018's read adapter. Now
also depends on TICK-034 (booking) and TICK-036 (cancellation), both done --
see "Re-scoped" below.

## Scope narrowed (2026-08-20)

This ticket bundled book + reschedule + cancel. Investigation (mirroring TICK-017's
own gap-resolution) found booking and cancel-by-status are both buildable --
`AppointmentService::insert()` and `AppointmentService::updateAppointmentStatus()`
are real, callable OpenEMR business logic, the same class of mechanism TICK-017
used. Split out as **TICK-031** (built as TICK-034/036). Reschedule alone had
no such call path and stayed blocked here -- OpenEMR v8.3.0's AppointmentService
has no update method for an existing appointment's date/time/duration, only
~150 lines of inline SQL in the legacy `interface/main/calendar/add_edit_event.php`
page, not a callable service; implementing an in-place reschedule would mean
new raw `pc_event` writes, exactly the workaround `evidence/TICK-001/ENDPOINT_MATRIX.md`
rejects.

## Re-scoped (2026-08-20): reschedule as cancel-then-rebook

Re-examined now that TICK-034 (booking) and TICK-036 (cancellation) both
ship real, callable OpenEMR business logic. FR-28 requires appointment
actions to "use the booking, rescheduling, cancellation, notice, and
eligibility rules already enforced by OpenEMR" -- since OpenEMR has no
distinct reschedule-specific enforcement to defer to, composing the two
rules it *does* enforce (booking, cancellation) is a faithful reading of
FR-28, not a workaround. FR-13 only requires "reschedule an existing
appointment to an open slot" -- it does not require preserving the original
appointment's OpenEMR id, so a new appointment record for the new slot is an
acceptable outcome.

**This is not atomic across the two OpenEMR calls** (they are separate REST
requests, not one DB transaction) -- the acceptance criteria below exist
specifically to make the partial-failure case (old appointment cancelled,
new one fails to book) honest rather than silently lossy.

**Depends on TICK-039 (open):** live verification found that chat's cancel
*intent* is not reliably selected by the planning LLM even when a real,
available appointment is in context. Reschedule must be implemented as its
own dedicated `reschedule` plan intent -- taking both a target `slot_token`
and the existing `appointment_token` together in one plan output and calling
`BookingService`/`CancellationService` directly server-side -- rather than by
routing through the separately-selected chat `cancel` intent TICK-039 shows
is broken; this sidesteps that specific bug by construction. Still listed as
a dependency because both intents draw on the same underlying appointment-
discovery/token mechanism, and TICK-039's root cause isn't confirmed yet --
until it lands, treat any assumption that "cancellation is fully reliable
end-to-end" as unproven for reschedule too.

## Acceptance Criteria

- [ ] A patient can reschedule an existing appointment through a dedicated
      `reschedule` plan intent that composes the real
      `CancellationService.cancel()` (TICK-036) and `BookingService`/booking
      call (TICK-034) directly -- no new raw SQL, no new OpenEMR write path,
      and no dependency on the separately-selected chat `cancel` intent path.
- [ ] The new slot is booked and confirmed by OpenEMR *before* the original
      appointment is cancelled, so a slot that's no longer open (already
      taken, stale token, etc.) never leaves the patient with both
      appointments cancelled and nothing rebooked.
- [ ] If cancelling the original appointment fails after the new one was
      successfully booked, the patient is told plainly that the new
      appointment is confirmed but the old one may still be active, and to
      contact the clinic if it isn't cancelled -- never silently drop or
      paper over that state.
- [ ] The assistant never claims a reschedule succeeded before both real
      OpenEMR calls it depends on have actually returned success.

## Testing

Run synthetic OpenEMR end-to-end reschedule operations against the local
pinned Docker stack: happy path (book-then-cancel both succeed) and
stale/conflicting target slot (book fails, original appointment untouched)
must both be proven live, with real OpenEMR responses. Cancellation failing
after a successful rebook is harder to force live on demand (no established
pattern in this repo for deterministically triggering a genuine, non-mocked
OpenEMR failure on the second call) -- an integration test with a mocked
adapter response for that specific edge case is acceptable, provided the
happy-path and conflicting-slot cases are still proven live. CI must be
green.

## Out of Scope

Physical deletion, staff scheduling, or AI-defined policy.
