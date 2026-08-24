---
id: TICK-063
title: "feat(chat): route every turn through the local model and execute its tool call"
type: feature
epic: EPIC-09
priority: P1
estimate: L
depends_on: [TICK-060, TICK-061, TICK-062]
labels: [llm, chat, backend]
source: [FR-33, FR-35]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/128
builder_commit: 138a592
---
## Context

`docs/LOCAL_LLM_SPEC.md` D9, D10, D16. This inverts the routing.

Today `chat_turn` (`ai_server/app/main.py`) steers PHI-bearing turns *away* from the
model: `onboarding_mode()` and `address_update_mode()` intercept them for
deterministic handlers, and everything else goes to Groq. After this ticket the
local model is the front door for every message and decides what happens, because
it is allowed to see patient data.

**The model's judgement selects an action. It never gates egress (D10).** If it
decides general knowledge is needed, that routes through TICK-064's restatement
path, where this codebase constructs the outbound payload and Presidio screens it. A
wrong routing decision picks the wrong action; it cannot place patient data in an
outbound request.

Turn shape under D16: the routing inference must complete before anything can
stream, so a visible pause before the first token is expected and accepted. Streaming
already exists on the client Protocol.

## Acceptance Criteria

- [ ] Every turn goes to the local model first. No message is routed by matching
      against phrasings, and no intent is detected by pattern before the model sees
      it -- that mechanism is what this work exists to remove.
- [ ] The model's tool call is executed through TICK-060's surface, with writes
      passing TICK-061's validation and the confirmation step.
- [ ] The reply streams (D16). The pre-stream pause is measured and recorded rather
      than hidden.
- [ ] Multi-turn state -- a half-finished address, an onboarding position, a pending
      confirmation -- survives across turns and is not reconstructed by re-reading
      the transcript with a pattern.
- [ ] A patient changing their mind mid-flow is handled by the model rather than by
      a cancel keyword: "actually, make it 2004" during a confirmation does the
      right thing.
- [ ] Nothing the patient typed reaches Groq on any path (FR-34). Asserted by test,
      not by inspection.
- [ ] Existing capability is preserved: booking, cancelling, listing appointments,
      onboarding answers, address and demographics updates, and OCR confirmation all
      still work, measured by TICK-062's corpus.

## Testing

Integration tests per capability driving real turns through the model against the
local backend, asserting the tool call, the validation outcome and the write. An
explicit test that no outbound Groq request contains patient-typed text. Then live
verification against the local Docker topology with a real seeded patient, recorded
under `evidence/TICK-063/`. CI must be green.

## Out of Scope

Deleting the deterministic handlers (TICK-065) -- they stay in the tree until this is
proven, and are dead code in between. The restatement path (TICK-064).
