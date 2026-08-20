---
id: TICK-034
title: "feat(chat): replace the no-op tool with real booking and cancellation"
type: feature
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-010, TICK-031]
labels: [chat, scheduling, langgraph]
source: [FR-11, FR-12, FR-14, FR-16, FR-20]
status: todo
---

## Context

`ai_server/app/chat.py`'s `NoActionTool` is the only `AuthoritativeTool`
`ChatService` ever constructs, and it performs no OpenEMR operation for any
planned intent -- every chat turn ends in "No scheduling action is available
yet in this demo." regardless of what the user asks. Its own docstring
explains why: "Booking, rescheduling, and cancellation are unimplemented
(TICK-020 is blocked)". That was true when TICK-010 built the planning/
streaming pipeline, but TICK-020 was later narrowed and its booking/cancel
scope split into TICK-031, which landed a real, tested
`ai_server.scheduling.booking.BookingService` and
`ai_server.scheduling.cancel.AppointmentCancelAdapter` -- neither of which
`chat.py` calls. Confirmed live 2026-08-20: a real patient chat session
reaches Groq, gets a valid plan, and still always receives the same
no-action summary, for `book` and `cancel` intents alike, even though the
services that could fulfill them exist and pass their own tests.

Open slot discovery (`ai_server.scheduling.slots.SlotDiscoveryService`,
`AnonymousSlotStore`) is also unwired: `ChatService._payload()` hardcodes
`scheduling_context.open_slots = []`, so the model never has real slots to
offer regardless of what a tool could do.

Reschedule has no equivalent backend (TICK-020 stays permanently blocked
for that one operation only) and must keep returning the no-action summary.

## Acceptance Criteria

- [ ] `ChatService._payload()` populates `scheduling_context.open_slots` from
      `SlotDiscoveryService` for the logged-in patient, not a hardcoded
      empty list.
- [ ] A new `AuthoritativeTool` implementation executes `book` plans (with a
      `slot_token`) through `BookingService`, `cancel` plans through
      `AppointmentCancelAdapter`, and falls back to the existing no-action
      summary only for `reschedule` and any intent missing a required field
      (e.g. `book` without a `slot_token`).
- [ ] A successful book or cancel produces a `public_summary` describing only
      the OpenEMR-confirmed outcome (FR-20); a conflict, stale slot, or
      already-cancelled response is reported clearly and invents no
      commitment.
- [ ] `main.py`'s `_build_chat_service` constructs the real tool (with the
      OpenEMR-facing HTTP client already used for OAuth/health, verify=False
      per the established local self-signed-cert exception) instead of
      `NoActionTool`, for every environment where the required OpenEMR
      settings are present.

## Testing

Unit-test the new tool against fake `BookingService`/`AppointmentCancelAdapter`
implementations for success, conflict, and already-cancelled cases, plus the
existing reschedule/missing-field fallback. Run a live end-to-end chat turn
against the local Docker stack (real patient login, real slot, real book,
real cancel) and capture the evidence, matching TICK-031's own live-proof
bar. CI must be green.

## Out of Scope

Reschedule (TICK-020, permanently blocked -- no OpenEMR service method
exists). Guided onboarding (TICK-035). Changing `BookingService`/
`AppointmentCancelAdapter` themselves.
