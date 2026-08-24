---
id: TICK-067
title: "spike(llm): establish how the model handles the turns no capability covers"
type: spike
epic: EPIC-09
priority: P2
estimate: M
depends_on: [TICK-062]
labels: [llm, discovery]
source: [FR-33]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/132
builder_commit: ac120a4
---
## Context

`docs/LOCAL_LLM_SPEC.md` D9, D15. The eval corpus measures the capabilities that
map to tools. It does not measure what happens on everything else -- and under D9
the model is the front door for every message, so "everything else" reaches it.

A patient in a behavioural-health intake will say things no tool covers: distress,
frustration, a question about their condition, a request for medical advice, or
something that is not a request at all. The current system has a deliberate,
approved answer for one of these -- `ONBOARDING_CONTRACT.md`'s supportive content
for a long pause, an upload failure, or explicit distress intent, which is absent
without a trigger. A 7-8B model with a general instruct tune has its own opinions
about all of it.

This is a spike because the deliverable is a finding, not a feature. The question is
what the model does today, unprompted, and what has to be constrained before it
speaks to patients. A negative result -- "it gives clinical reassurance it should
not" -- is a complete and valuable outcome.

## Acceptance Criteria

- [ ] A written finding covering, at minimum: expressions of distress, requests for
      medical or clinical advice, questions about medication, frustration or abuse,
      off-topic conversation, and attempts to make the assistant act outside its
      role.
- [ ] Each case records what the model actually said, verbatim, against the pinned
      model from TICK-062 -- not a summary, and not what a prompt was supposed to
      make it say.
- [ ] The finding states which behaviours must be constrained before patient
      exposure and which are acceptable, with a reason for each.
- [ ] The existing approved supportive content is compared against what the model
      does unprompted. Where the model would override or contradict an approved
      response, that is called out as a contract violation, not a style difference.
- [ ] Whether prompt constraints alone are sufficient, or whether some categories
      need deterministic interception before the model sees them, is answered
      explicitly. That answer may reopen D9 for a narrow class of turns, and saying
      so is the point of the spike.
- [ ] Any change the finding demands becomes its own ticket rather than being folded
      in here.

## Testing

Discovery, so the evidence is the deliverable. Record the transcripts under
`evidence/TICK-067/` with the model, quantisation and backend that produced them,
since the answer is specific to all three. No production code is expected to change.

## Out of Scope

Implementing the constraints the finding recommends. Changing
`ONBOARDING_CONTRACT.md`, which is product-approved and would need its own approval
to alter.
