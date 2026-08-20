---
id: TICK-035
title: "feat(chat): route chat turns into the guided onboarding flow"
type: feature
epic: EPIC-06
priority: P1
estimate: L
depends_on: [TICK-010, TICK-017]
labels: [chat, onboarding, langgraph]
source: [FR-5, FR-6, FR-7, FR-8, FR-27, FR-30]
status: done
builder_commit: e27b5e6
---
## Context

`ai_server/onboarding/flow.py`'s `OnboardingFlow` (field-by-field capture,
local validation, live OpenEMR draft checkpointing via
`AssessmentDraftAdapter`, and completion through `confirm_identity`) is real,
built, and covered by 138 passing tests (`test_onboarding_flow.py`,
`test_onboarding_fields.py`, `test_onboarding_draft_client.py`,
`test_onboarding_triggers.py`). `ai_server.app.auth.SessionStore` already has
the `save_cursor`/`load_cursor` pair `OnboardingFlow` needs for its
`OnboardingCursor` (ARCHITECTURE.md Sec. 5's "non-patient workflow cursor"),
and nothing else calls either method. Confirmed live 2026-08-20: no route in
`main.py`, and no branch in `chat.py`'s `ChatService.stream_reply()`, ever
constructs an `OnboardingFlow` or reads a session's cursor -- every chat turn
unconditionally goes through the scheduling-only `GroqWorkflow` (whose
`SYSTEM_PROMPT` only mentions scheduling, and whose `PlanningOutput.intent`
enum has no onboarding value), so a real patient asking to complete
onboarding gets a scheduling-flavored "no action available" response every
time, regardless of what they ask. This is the literal product name (PRD.md:
"AI-assisted behavioral-health onboarding in OpenEMR") not being reachable
from the one UI that exists for it.

`OnboardingFlow`'s own docstring is explicit that its field capture,
validation, and checkpointing are deterministic local operations that never
call an external model -- this is architecturally a separate turn-handling
path from `GroqWorkflow`, not another `AuthoritativeTool` plugged into the
scheduling planner.

## Acceptance Criteria

- [ ] `POST /api/chat` determines per-session onboarding state from
      `SessionStore.load_cursor` (present cursor with an incomplete draft =
      mid-onboarding; absent = not yet started) rather than always
      dispatching to `GroqWorkflow`.
- [ ] A session with no active onboarding draft and no explicit request to
      start onboarding continues to use the existing scheduling `GroqWorkflow`
      path unchanged (no regression to TICK-034's scheduling behavior).
- [ ] A session in onboarding mode routes each turn through `OnboardingFlow`:
      field prompts, validation rejections (`FieldCheckpointRejected`), and
      completion (`AssessmentRecord`) all reach the iframe as streamed
      responses, with no patient-supplied field value ever sent to Groq.
- [ ] `SessionStore.save_cursor`/`load_cursor` persist the draft position
      across an AI-server restart (FR-30's own draft-recovery requirement,
      proven the same way TICK-017 proved OpenEMR-side restart recovery).
- [ ] Friction-trigger supportive content (`ai_server/onboarding/triggers.py`)
      surfaces through the same streamed-response path, not a separate
      endpoint.

## Testing

Unit-test the mode-routing decision (no cursor, mid-draft cursor, completed
cursor) with fake session/draft state. Run a live end-to-end onboarding
conversation against the local Docker stack -- start, an invalid field
rejected, a valid field checkpointed, restart the AI server mid-flow, resume,
complete -- and confirm the completed record lands in OpenEMR through the
real endpoint, matching TICK-017's own live-proof bar. CI must be green.

## Out of Scope

Changing `OnboardingFlow`, `AssessmentDraftAdapter`, or the OCR/identity
confirmation pipeline (TICK-014/TICK-016) themselves. Scheduling (TICK-034).
