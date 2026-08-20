---
id: TICK-031
title: "feat(scheduling): book and cancel appointments through OpenEMR"
type: feature
epic: EPIC-07
priority: P1
estimate: L
depends_on: [TICK-018, TICK-019]
labels: [scheduling, openemr]
source: [FR-12, FR-14, FR-16, FR-20, FR-28, NFR-11]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/58
builder_commit: 261cd46
---
## Context

Split from TICK-020 (2026-08-20): that ticket bundled book + reschedule + cancel
and was blocked wholesale on the assumption that none had a safe implementation
path. Investigation (the same approach TICK-017 used to resolve its own platform
gap) found that's only true for reschedule. Booking and cancel-by-status both have
a real, callable path:

- **Book**: `evidence/TICK-001/ENDPOINT_MATRIX.md` already records
  `POST /api/patient/{pid}/appointment` (Standard API) as "Supported locally" --
  proven in TICK-001's own probe, just never wired into `ai_server`. TICK-019
  already produces the anonymous slot tokens this ticket resolves to a real
  OpenEMR slot before booking.
- **Cancel-by-status**: no Standard/FHIR route exists for this, but OpenEMR's own
  `AppointmentService::updateAppointmentStatus($eid, $status, $user)`
  (`src/Services/AppointmentService.php:591`) is real, callable business logic --
  the same method the classic staff calendar UI's cancel action calls -- not a
  thin SQL wrapper. `list_options` (`apptstat`) has real cancelled-with-history
  status codes (`x`, `%`) distinct from `DELETE`. Reachable the same way TICK-017
  added a route: register `PUT /portal/patient/appointment/:eid` (or similar)
  through `OpenEMR\Events\RestApiExtend\RestApiCreateEvent`, no core file touched,
  and add the ownership check `AppointmentRestController::delete()` already does
  today (verify the appointment's `pid` matches the caller's own bound patient
  before calling `updateAppointmentStatus`) -- `updateAppointmentStatus` itself
  has no such check built in and assumes a trusted caller.

Reschedule has no equivalent call path (only inline SQL in a legacy calendar page)
and stays on TICK-020, blocked.

## Acceptance Criteria

- [ ] A patient can book a genuine open slot (from TICK-019's anonymous tokens)
      through a deterministic OpenEMR call; the token resolves to the real slot
      server-side, never trusting a client-supplied OpenEMR identifier.
- [ ] A patient can cancel their own existing appointment by OpenEMR status
      update, retaining the record (never `DELETE`); cancelling another patient's
      appointment is impossible by construction, proven with a live cross-patient
      negative test (same discipline as `evidence/TICK-028/BINDING_MATRIX.md` and
      `evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md`).
- [ ] OpenEMR confirmation is required before any success response; conflict or
      stale-slot/already-cancelled responses are clear and create no invented
      commitment.
- [ ] Double-submitted or concurrent booking attempts produce no more than one
      confirmed appointment.

## Testing

Run synthetic OpenEMR end-to-end operations, stale-slot conflict, and
concurrent-final-slot tests against the local pinned Docker stack, plus a live
cross-patient negative test for cancel. CI must be green.

## Out of Scope

Reschedule (TICK-020, blocked). Physical deletion, staff scheduling, or
AI-defined policy.
