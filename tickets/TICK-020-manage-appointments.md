---
id: TICK-020
title: "feat(scheduling): reschedule an appointment through OpenEMR"
type: feature
epic: EPIC-07
priority: P1
estimate: L
depends_on: [TICK-018, TICK-019]
labels: [scheduling, openemr]
source: [FR-13, FR-20, FR-28, NFR-11]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/21
blocked_reason: "Narrowed 2026-08-20 to reschedule only (see note below): OpenEMR v8.3.0's AppointmentService has no update method for an existing appointment's date/time/duration -- only ~150 lines of inline SQL in the legacy interface/main/calendar/add_edit_event.php page, tangled with recurrence/multi-provider branching, not a callable service. Implementing reschedule would mean new raw pc_event writes, exactly the workaround evidence/TICK-001/ENDPOINT_MATRIX.md already rejects. Booking and cancel-by-status, this ticket's other two parts, are NOT blocked -- see TICK-031."
---
## Context

Appointment writes occur only after a deterministic OpenEMR call; the assistant may not claim success before that response.

`depends_on` still includes TICK-019 after the reschedule-only narrowing: moving an
existing appointment to a new time needs TICK-019's anonymous-slot discovery to find
a genuinely open target slot, the same as booking does, not just TICK-018's read
adapter.

## Scope narrowed (2026-08-20)

This ticket bundled book + reschedule + cancel. Investigation (mirroring TICK-017's
own gap-resolution) found booking and cancel-by-status are both buildable --
`AppointmentService::insert()` and `AppointmentService::updateAppointmentStatus()`
are real, callable OpenEMR business logic, the same class of mechanism TICK-017
used. Split out as **TICK-031** (`status: todo`, not yet built). Reschedule alone
has no such call path and stays blocked here; see `blocked_reason`.

## Acceptance Criteria

- [ ] A patient can reschedule an existing appointment through a deterministic
      OpenEMR call, with no invented commitment on conflict.

## Testing

Run synthetic OpenEMR end-to-end reschedule operations and stale-conflict tests against the local pinned Docker stack. CI must be green.

## Out of Scope

Physical deletion, staff scheduling, or AI-defined policy.
