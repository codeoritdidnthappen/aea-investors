---
id: TICK-061
title: "feat(chat): validate every model-proposed field before it can reach the record"
type: feature
epic: EPIC-09
priority: P1
estimate: M
depends_on: [TICK-060]
labels: [llm, chat, privacy, backend]
source: [FR-35, NFR-36]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/126
builder_commit: 9e75653
---
## Context

`docs/LOCAL_LLM_SPEC.md` D6. This is the safety-critical component of the whole
change: the only thing standing between a probabilistic model and a patient's
chart.

It exists because the current design already failed at exactly this seam. TICK-050
built confirm-then-write for addresses, and it still wrote
`"Update it to: 2002 Bridge Avenue"` into a record -- because the confirmation
step rendered whatever the parser produced instead of independently checking it.
`validate_address` accepted any non-empty string as a street. The safety net was a
mirror.

The validator must therefore be independent of whatever produced the value. It
holds when the model is wrong, when a prompt regresses, when the runtime is swapped
from Ollama to vLLM (D7), and it would have held for the regex parser too.

## Acceptance Criteria

- [ ] Every field of every writing tool is validated by application code that does
      not trust the model: a street looks like a street, a ZIP like a ZIP, a date
      like a plausible date, an appointment reference resolves to one this patient
      actually has.
- [ ] A field that cannot be validated is **refused**, never written and never
      silently corrected. Under NFR-36 a refusal is an acceptable outcome and a
      wrong write is not.
- [ ] The patient's confirmation shows the validated values, and the confirmation
      is a genuine second check rather than an echo of the proposal. A value the
      validator could not vouch for never reaches a confirmation prompt.
- [ ] `"Update it to: 2002 Bridge Avenue"` and its class -- a lead-in phrase, a
      trailing question, an answer to a different question -- are refused by the
      street validator. This is a committed regression test, named as such.
- [ ] Validation failures are legible to the patient: what was wrong and what to
      send instead, without exposing schema internals or model output.
- [ ] Validators are reusable and are the single authority. No writing path may
      construct a record value without passing through them.

## Testing

Table-driven unit tests per field type, with the observed real-world failure as an
explicit case. Property tests where the shape allows. An integration test asserting
that no writing tool can reach its service with an unvalidated value, by attempting
it. CI must be green.

## Out of Scope

The eval corpus that measures NFR-36 across realistic phrasings (TICK-062). The tool
schemas (TICK-060). Changing what the underlying services do once given a valid
value.
