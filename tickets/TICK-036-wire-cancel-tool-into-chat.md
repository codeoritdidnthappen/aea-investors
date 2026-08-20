---
id: TICK-036
title: "feat(chat): anonymously reference and cancel a real appointment"
type: feature
epic: EPIC-07
priority: P2
estimate: M
depends_on: [TICK-034]
labels: [chat, scheduling, langgraph]
source: [FR-9, FR-14, FR-15, FR-16, FR-20]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/73
builder_commit: 52979c1
---
## Context

Split from TICK-034 (2026-08-20) to keep booking's clean, already-solved path
(access token + `slot_token` -> `BookingService`) unblocked by cancellation's
separate, unsolved problem: `ai_server.scheduling.cancel.AppointmentCancelAdapter
.cancel(access_token, appointment_id)` needs a real OpenEMR appointment
identifier, but nothing anywhere resolves "which of the patient's own
appointments do they mean" from a chat message into one -- and the model must
never be trusted with (or asked to echo back) a raw OpenEMR identifier, the
same reason `AnonymousSlotStore` exists for booking rather than letting the
model name a real slot (TICK-019).

Reading the patient's own current appointments to show them is already
solved: TICK-018's adapter reads active appointments through mapped
endpoints, cancelled ones already omitted (FR-15). What's missing is the
booking side's own pattern applied to *existing* appointments instead of
open slots: a short-lived, single-purpose anonymous token identifying one of
the caller's own appointments, issued when appointments are read into
`scheduling_context`, resolved back to the real OpenEMR id only server-side
at cancel time -- never trusting a client- or model-supplied identifier,
exactly `AnonymousSlotStore`'s existing discipline (TICK-019), applied to a
different source list.

## Acceptance Criteria

- [ ] The patient's current, non-cancelled appointments are read (the
      existing TICK-018 adapter) and each is issued a short-lived, single-use
      anonymous token the same way `AnonymousSlotStore` issues slot tokens;
      only the token, never a real OpenEMR appointment id, reaches
      `scheduling_context` or the model.
- [ ] The token store resolves a token to its real appointment id
      server-side only, exactly once, and only for the session that
      originally received it -- proven with a live cross-patient negative
      test (same discipline as `evidence/TICK-028/BINDING_MATRIX.md`).
- [ ] `PlanningOutput`'s schema gains whatever field a `cancel` intent needs
      to carry this token (mirroring `slot_token`'s existing pattern:
      nullable, pattern-constrained, present only for the relevant intent).
- [ ] The scheduling `AuthoritativeTool` (TICK-034) executes `cancel` plans
      through `AppointmentCancelAdapter.cancel(access_token, appointment_id)`
      using the token's resolved id; a stale, already-resolved, or
      already-cancelled token produces a clear response and invents no
      commitment (FR-16).
- [ ] A cancelled appointment's `public_summary` never claims success unless
      OpenEMR's own response confirms the status change (FR-20).

## Testing

Unit-test the anonymous appointment-token store (issue, single-use resolve,
expiry, cross-session isolation) the same way `test_slot_discovery.py`
covers `AnonymousSlotStore`. Run a live end-to-end chat turn against the
local Docker stack (real patient login, list appointments, cancel one,
confirm it's gone from the list and remains in OpenEMR with cancelled
status) plus the live cross-patient negative test. CI must be green.

## Out of Scope

Reschedule (TICK-020, permanently blocked). Booking (TICK-034). Physical
deletion or staff-facing cancellation.
