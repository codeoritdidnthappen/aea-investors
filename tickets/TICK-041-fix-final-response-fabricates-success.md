---
id: TICK-041
title: "bug(chat): final-response model call can fabricate a false success on a real OpenEMR failure"
type: task
epic: EPIC-07
priority: P0
estimate: M
depends_on: [TICK-036, TICK-039]
labels: [chat, scheduling, safety, langgraph]
source: [FR-14, FR-15, FR-16, FR-20, NFR-11]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/85
---

## Context

Found live 2026-08-20 independently re-verifying TICK-039's fix end-to-end
(the build-agent that fixed TICK-039 could only prove the isolated planning
half live, with no browser tool or stored patient credentials in its
sandbox -- see `evidence/TICK-039/GROQ_REQUEST_EVIDENCE.md`'s own "What
remains unverified" section). This is the gap that section flagged.

A real patient login, a real seeded appointment (`pc_eid=7`, cancel-eligible,
`pc_apptstatus='-'`), and a real chat turn ("Can you cancel my upcoming
appointment?") produced:

```
Assistant: {"appointment_token":"appt_ZAMl7zCaPyttB6I0a1OC4LDD","cancellation":"confirmed"}
```

**The appointment was never cancelled.** `openemr_postcalendar_events.pc_apptstatus`
for `pc_eid=7` is still `-` after this turn, confirmed directly in the
database. OpenEMR's own access log for this exact request shows why:

```
PUT /apis/default/portal/patient/appointment/a28cfee8-f65a-488a-b186-253e2d609a7c -> 404
```

(UUID confirmed to exactly match `pc_eid=7`'s own `uuid` column and the
correct patient -- not a wrong-id mistake on the caller's side; root cause
of the 404 itself is a separate, narrower question, see "Also found" below.)

`ai_server/scheduling/cancel.py`'s `AppointmentCancelAdapter.cancel()`
correctly turns a `404` into `AppointmentNotFoundError`, and `chat.py`'s
`BookingTool._execute_cancel()` correctly catches that and returns an honest
`AuthoritativeToolResult(public_summary="OpenEMR could not confirm that
cancellation just now, so the appointment was not cancelled. Please try
again.")` -- this part of the pipeline behaved exactly as designed.

The bug is downstream, in `GroqWorkflow.respond()` (`ai_server/llm/groq.py:260-280`):
after the tool executes, a **second** Groq call is built via `_final_payload()`
whose system prompt is `"Describe only the authoritative scheduling result.
Do not add facts."` and whose user message is literally `result.public_summary`
(the honest failure text above). This second call's *streamed output* is
what the patient actually sees -- and nothing in this codebase checks that
output against `result` before it reaches the user. The model was given an
honest failure message and, despite the "do not add facts" instruction,
streamed back a fabricated, structurally-unrelated JSON success claim.

This directly violates the project's own stated, repeated invariant --
"Only OpenEMR's validated response can produce a booking, rescheduling, or
cancellation" (`ARCHITECTURE.md`) and "the assistant may not claim success
before that response" (TICK-020's own Context section) -- and it is not a
narrow prompt-wording issue: there is currently **no code-level guardrail**
between the tool's real, deterministic result and what gets streamed to the
patient. A prompt fix alone (tightening the instruction further) does not
close this class of bug; the model can always be asked one more time to
misbehave. A future onboarding/booking success claim is just as exposed to
this same gap, not only cancellation.

## Also found (separate, narrower issue)

The underlying `404` itself is unexplained and should be root-caused as part
of or alongside this fix: `AppointmentCancelService::forPatient()`
(`openemr_modules/aeai-portal-chat/src/Service/AppointmentCancelService.php:66`)
calls `AppointmentService()->search(['puuid' => $patientUuid, 'pc_uuid' =>
$auuid])` and returns zero rows even when both values were confirmed correct
and consistent directly in the database (`patient_data.uuid` for pid 1 matches
the `puuid` passed, and `pc_eid=7`'s own `uuid` column matches the `pc_uuid`
passed, `pc_pid` correctly links the two). Not yet isolated whether this is
an `AppointmentService::search()` behavior this module's call doesn't account
for, or something else. Not blocking this ticket's primary fix (the
fabricated-success gap matters regardless of why the underlying call failed
-- a real 404 for any reason must never be reported as a success), but should
be fixed in the same pass since it's what triggered this specific repro.

## Acceptance Criteria

- [ ] The patient-visible response for any authoritative scheduling outcome
      (book, cancel, reschedule) can never claim success unless `result`
      (the tool's real, deterministic `AuthoritativeToolResult`) represents
      one -- enforced in code, not by prompt wording alone. Acceptable
      approaches include (not prescriptive): skip the second model call
      entirely for authoritative outcomes and stream `result.public_summary`
      verbatim; or validate the streamed text against `result` before
      forwarding it and fall back to the verbatim summary on any mismatch.
- [ ] A live-reproducing test proves a real OpenEMR failure (404, or any
      other non-success) can never result in a success-shaped message
      reaching the patient, using the exact repro in this ticket's Context
      (or an equivalent) -- not just a unit test against a mocked "well-
      behaved" model response.
- [ ] The separate `AppointmentCancelService::forPatient()` 404 is
      root-caused and fixed (or documented as a genuine, different platform
      limitation if that's what's found).
- [ ] Re-run this ticket's exact repro live after the fix: real login, real
      appointment, real cancel request -- confirm both the message shown to
      the patient and `pc_apptstatus` in the database agree with each other.

## Testing

Live verification against the local Docker topology: the exact repro above,
plus a genuine successful cancellation (to confirm the fix doesn't turn a
real success into a false failure either -- symmetry matters as much as the
one-directional bug found here). CI must be green.

## Out of Scope

Booking and reschedule's own separate, already-known blockers (TICK-040's
staff-ACL route gap; ADR-3's no-candidate-slot gap) -- this ticket is about
the response-integrity guardrail itself, which those tickets' fixes will
also depend on once they reach a real OpenEMR call.
