# TICK-039 — live Groq request/response evidence, 2026-08-20

**Executed:** direct calls from this build worker's sandbox to the real Groq API
(`https://api.groq.com/openai/v1/chat/completions`, real `GROQ_API_KEY` from the
already-running local Docker topology's `deploy/local/.env`, pinned model
`openai/gpt-oss-120b`) using the project's own `ai_server.llm.groq.HttpGroqClient`
and `ai_server.privacy.gate.OutboundPayload`/`SchedulingContext`/`SchedulingRules`
classes. No browser or OpenEMR OAuth session was available in this sandbox (no
patient portal credentials on disk, per the redaction policy in
`deploy/local/PATIENT_AUTH.md`, and no browser-automation tool), so this isolates and
proves the Groq-planning half of the bug -- the half the ticket's Context section
says was "not yet isolated" -- with real, reproducing traffic rather than a
hypothesis. It does not by itself re-prove the OpenEMR-side write (see "What remains
unverified" below).

Reproducing turn used throughout: system prompt = the real `SYSTEM_PROMPT` constant
from `ai_server/app/chat.py`; user message = `"Can you cancel my upcoming
appointment?"`; `scheduling_context.current_appointments` = one real-shaped
`AnonymousAppointment` (`appt_repro0000000000000000000001`, tomorrow 10:00 UTC-5);
`scheduling_rules.cancellation_enabled = True` -- the same shape TICK-024's live E2E
finding confirmed genuinely reaches `ChatService._payload()` for the seeded
`AverySubjecttest1` appointment.

## 1. Before the fix: the exact bug, live

Called `HttpGroqClient.complete()` (pre-fix, `ai_server/llm/groq.py` at
`_request_body` sending only `model`/`messages`/`stream`) with the reproducing
payload above.

**Request body actually sent to Groq** (all of it -- `scheduling_context` and
`scheduling_rules` are simply absent):

```json
{
  "model": "openai/gpt-oss-120b",
  "messages": [
    {"role": "system", "content": "<SYSTEM_PROMPT>"},
    {"role": "user", "content": "Can you cancel my upcoming appointment?"}
  ],
  "stream": false,
  "response_format": {"type": "json_schema", "json_schema": {"...": "PlanningOutput schema"}}
}
```

**Groq's real response:**

```json
{"intent":"cancel","slot_token":null,"appointment_token":null}
```

Groq correctly read the user's intent from the text alone -- `intent="cancel"` -- but
had no `current_appointments` to select from, so `appointment_token` is `null`. This
is exactly why `BookingTool.execute()`'s `plan.intent == "cancel" and
plan.appointment_token is not None` (`chat.py:127`) never took the cancel branch and
fell through to `NoActionTool`'s fixed fallback text. **Confirmed: a wiring bug, not
a model prompt-following gap** -- the model behaved exactly as its actual input
justified.

## 2. An attempted literal fix that Groq itself rejects

`ARCHITECTURE.md`'s "Approved external request shape" (and `OutboundPayload`'s own
docstring, which calls itself "The architecture-approved external request shape")
show `scheduling_context`/`scheduling_rules` as sibling top-level JSON keys next to
`messages`. Sending exactly that shape live:

**Request body:** `model`, `messages`, `stream`, `response_format`, plus top-level
`scheduling_context` and `scheduling_rules` keys.

**Groq's real response:**

```
STATUS: 400
BODY: {"error":{"message":"property 'scheduling_context' is unsupported","type":"invalid_request_error"}}
```

Groq's real chat completions endpoint hard-rejects any top-level request property it
does not recognize. Even had it not, an OpenAI-compatible completions endpoint only
ever feeds `messages[].content` to the model -- an unrecognized sibling key is never
part of what the model reads regardless. This rules out the literal architecture-doc
shape as an actual wire fix and explains why matching `OutboundPayload`'s own schema
more closely would have made things *worse* (every planning call, not just
cancellation ones, would 400).

## 3. The actual fix: fold context into the system message content

Appended `scheduling_context`/`scheduling_rules` as JSON text onto the system
message's `content` (the one channel Groq's endpoint genuinely passes to the model),
leaving `OutboundPayload.messages` (privacy gate, message-shape validation, and every
existing test that reads it) completely unchanged -- only `HttpGroqClient`'s wire
serialization changed.

**Request body actually sent to Groq** (`messages[0].content` now ends with
`scheduling_context: {...}` and `scheduling_rules: {...}` JSON):

```
STATUS: 200
```

**Groq's real response:**

```json
{"intent":"cancel","slot_token":null,"appointment_token":"appt_repro0000000000000000000001"}
```

The model now correctly resolves the one real appointment token available in
`scheduling_context.current_appointments`. Re-ran this exact call through the
project's actual (post-fix) `HttpGroqClient.complete()` -- not a standalone
reimplementation -- with the same result:

```json
{"appointment_token":"appt_repro0000000000000000000001","intent":"cancel","slot_token":null}
```

## What remains unverified

This evidence isolates and fixes the Groq-planning half of the bug with real,
reproducing live traffic. `BookingTool.execute()` taking the `cancel` branch to
`CancellationService.cancel()` once `appointment_token` is populated was already
unit-proven against a fake `CancellationService` before this ticket
(`ai_server/tests/test_booking_tool.py`, TICK-036), and this ticket adds a full
`GroqWorkflow` -> `BookingTool` -> `CancellationService` pipeline test
(`ai_server/tests/test_groq_scheduling_context_wiring.py`) proving that wiring still
holds with the fixed request-building code. Re-driving the *entire* turn through a
real patient OAuth login and a real OpenEMR-side appointment status change (as
`evidence/TICK-024/DESKTOP_E2E_EVIDENCE_2.md` did, "real desktop Chrome via browser
automation") needs a patient portal login this sandboxed build worker does not have
(no stored credentials, per `deploy/local/PATIENT_AUTH.md`'s redaction policy) and a
browser-automation tool this session was not given. A follow-up live pass -- ideally
re-running TICK-024's exact Finding 2 repro against the already-seeded
`AverySubjecttest1` appointment now that this fix is in place -- would close that
remaining gap.

**Update, live-verified 2026-08-20 (after this ticket's fix landed and the running
`local-ai-server-1` container was rebuilt to pick it up):** the planning half fixed
here is confirmed correct live -- the model now selects `intent=cancel` with the
real `appointment_token`, and a real `PUT` to OpenEMR's cancel route is genuinely
made. But that call returned a real `404`, and the patient-visible response still
claimed `"cancellation":"confirmed"` -- a fabrication produced by a *second*,
separate Groq call (`GroqWorkflow.respond()`'s post-tool "describe the result" step)
that this ticket's fix never touched. `pc_apptstatus` was confirmed unchanged in the
database. This is a distinct, more severe bug than TICK-039 was scoped to fix --
filed as **TICK-041** (P0). TICK-039's own narrow claim (the model reliably selects
a real appointment token once it's actually present in its input) is true and
proven; it just isn't sufficient on its own for a trustworthy end-to-end result.
