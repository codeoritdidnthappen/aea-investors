---
id: TICK-020
title: "feat(scheduling): book reschedule and cancel through OpenEMR"
type: feature
epic: EPIC-07
priority: P1
estimate: L
depends_on: [TICK-018, TICK-019]
labels: [scheduling, openemr]
source: [FR-12, FR-13, FR-14, FR-16, FR-20, FR-28, NFR-11]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/21
blocked_reason: "TICK-020 requires reschedule and cancel-by-OpenEMR-status, but evidence/TICK-001/ENDPOINT_MATRIX.md documents no such endpoint exists on the pinned OpenEMR v8.3.0 release and explicitly names TICK-018\u2013TICK-020 as forbidden from implementing these gap rows (no delete/recreate or DELETE-based emulatio"
---
## Context

Appointment writes occur only after a deterministic OpenEMR call; the assistant may not claim success before that response.

## Acceptance Criteria

- [ ] A patient can book an open slot, reschedule an existing appointment, and cancel by OpenEMR status update.
- [ ] OpenEMR confirmation is required before any success response.
- [ ] Conflict or stale-slot responses are clear and create no invented commitment.
- [ ] Double-submitted or concurrent booking attempts produce no more than one confirmed appointment.

## Testing

Run synthetic OpenEMR end-to-end operations, stale conflict, and concurrent-final-slot tests against the local pinned Docker stack. CI must be green.

## Out of Scope

Physical deletion, staff scheduling, or AI-defined policy.
