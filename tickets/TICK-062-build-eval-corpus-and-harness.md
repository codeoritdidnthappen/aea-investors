---
id: TICK-062
title: "feat(eval): build the acceptance corpus and harness, and pick the model with it"
type: feature
epic: EPIC-09
priority: P1
estimate: L
depends_on: [TICK-059, TICK-060, TICK-061]
labels: [llm, eval, backend]
source: [FR-33, NFR-36]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/127
builder_commit: null
---
## Context

`docs/LOCAL_LLM_SPEC.md` D8, D11, D15. Two runtimes means two behaviours: Ollama
serves quantised GGUF locally, vLLM serves fp16 or AWQ when deployed. Same model
name, different numerics, different outputs. Without a corpus, "does this handle
address updates correctly" is unanswerable until a patient answers it -- which is
how `"Update it to: 2002 Bridge Avenue"` was found.

D11 fixes the model class at ~7-8B instruct but not the model. This ticket picks it,
on measured results rather than reputation, and the corpus is what does the picking.

**Two bars, deliberately different (D15):**

- **Writes: zero wrong, across the whole corpus.** A field reaching the record is
  correct or refused. This is NFR-36 and it is not a percentage.
- **Understanding: imperfect is acceptable.** Failing to grasp a request costs a
  retry. It is held to a stated threshold, not to zero.

## Acceptance Criteria

- [ ] A corpus of realistic patient phrasings per capability, each with expected
      structured output. It includes the phrasings that broke the parsers -- lead-in
      phrases, an answer to a different question, a correction mid-sentence, a
      partial address, a refusal, a question instead of an answer.
- [ ] The corpus contains only synthetic data (NFR-1) and no real patient
      information, and is safe to commit.
- [ ] The harness runs against whichever backend `LLM_PROVIDER` selects, and
      reports the two bars separately. A single blended score is not acceptable
      output: it prices a corrupted record like a misunderstood question.
- [ ] A wrong write is reported individually with its input, expected value, and
      produced value. Under NFR-36 that is a release blocker, so it must be legible
      enough to act on.
- [ ] The harness runs the same corpus against Ollama and vLLM and reports where
      they disagree. D7 makes divergence structural, so it is measured, not assumed
      away.
- [ ] The model and quantisation are chosen from these results and recorded with the
      numbers that justified them, in `AI_USAGE.md` (NFR-21) and the spec.
- [ ] The harness is runnable in CI against a small deterministic subset, so a
      prompt change cannot regress the write bar unnoticed. The full run may stay
      manual if runtime demands it, and the runbook says which is which.

## Testing

The harness is itself tested: a known-good fixture passes, a seeded wrong write is
caught and reported as a blocker, and a seeded misunderstanding is reported under
the softer bar. Then the real run, with results recorded under `evidence/TICK-062/`,
including the backend comparison. CI must be green.

## Out of Scope

Changing routing (TICK-063). Prompt iteration beyond what selecting a model needs --
the corpus exists to measure prompts, and improving them is ongoing work rather than
this ticket's completion criterion.
