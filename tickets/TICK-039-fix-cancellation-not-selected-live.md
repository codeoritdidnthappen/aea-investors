---
id: TICK-039
title: "bug(chat): cancellation intent never selects a real, available appointment"
type: task
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-036, TICK-037]
labels: [chat, scheduling, langgraph]
source: [FR-9, FR-14, FR-15, FR-16, FR-20]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/77
---

## Context

Found live 2026-08-20 during TICK-024's desktop E2E re-attempt. A real
appointment was seeded for the logged-in synthetic patient (`AverySubjecttest1`,
`pc_eid=7`, tomorrow 10:00, via `AppointmentService::insert()` -- the same
real OpenEMR business-logic call the booking tool itself uses, not a raw-SQL
workaround) specifically to exercise cancellation live for the first time
(TICK-036 had never been proven live end-to-end).

`GET /apis/default/fhir/Appointment` was confirmed to return a real,
non-empty bundle (1240 bytes, vs. 195 bytes for an empty bundle observed
before the appointment existed) -- confirming `OpenEmrScheduleAdapter.
active_appointments()` and `AppointmentDiscoveryService.current_appointments()`
both function and the appointment reaches `ChatService._payload()`'s
`scheduling_context.current_appointments` list, exactly as `ai_server/app/
chat.py`'s own code shows it should (`_SCHEDULING_RULES.cancellation_enabled
= True`).

Despite that, asking the chat "Can you cancel my upcoming appointment?" (a
real, live login and chat turn, not a scripted probe) returned the exact
`NoActionTool` fallback text ("No scheduling action is available yet in this
demo.") -- the same fallback a `book` intent with no candidate slots also
produces. `BookingTool.execute()` only calls `_execute_cancel()`
when `plan.intent == "cancel" and plan.appointment_token is not None`
(`chat.py:127`); since neither branch was taken, either the LLM (Groq
`openai/gpt-oss-120b`) never selected `intent="cancel"` for this phrasing, or
it selected `cancel` but did not populate `appointment_token` with the one
real token that was available in `scheduling_context`. Not yet isolated
which -- needs the actual outbound payload sent to Groq and the actual
structured `PlanningOutput` it returned for this turn (not yet captured).

`SYSTEM_PROMPT` (`chat.py:46-56`) does instruct the model correctly in
principle ("To cancel, reference one of the caller's own current
appointments by its appointment_token"), so this may be a genuine prompt/
model-following gap rather than a wiring bug -- but that's a hypothesis, not
yet confirmed.

## Acceptance Criteria

- [ ] Root cause confirmed with direct evidence (the actual Groq request/
      response for a reproducing turn, or equivalent), not just the
      hypothesis above.
- [ ] A genuine patient login, with a real existing appointment, followed by
      a natural-language cancellation request in chat, results in
      `CancellationService.cancel()` actually being invoked and a real
      OpenEMR-side status change -- proven live, not just against a unit test.
- [ ] `NoActionTool`'s fallback is reserved for turns that genuinely have no
      matching real appointment/intent, not silently swallowing a turn that
      had one.

## Testing

Live verification against the local Docker topology: a real patient login,
a real seeded appointment (via `AppointmentService::insert()`, not raw SQL),
and a real natural-language cancellation chat turn, through to a confirmed
OpenEMR-side status change. CI must be green.

## Out of Scope

Booking (still intentionally `booking_enabled=False` for this demo,
unrelated) and rescheduling (TICK-020, permanently blocked).
