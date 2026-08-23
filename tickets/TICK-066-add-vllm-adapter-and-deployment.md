---
id: TICK-066
title: "feat(deploy): add the vLLM backend and prove it agrees with the development one"
type: feature
epic: EPIC-09
priority: P2
estimate: L
depends_on: [TICK-062, TICK-063]
labels: [deploy, llm, backend]
source: [FR-33, NFR-36]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/131
builder_commit: null
---
## Context

`docs/LOCAL_LLM_SPEC.md` D5, D7. Development runs Ollama on Apple Silicon;
deployment runs vLLM. This ticket adds the second backend and, more importantly,
measures whether it behaves like the first.

The risk is not the adapter, which is small. It is that Ollama serves quantised
GGUF and vLLM serves fp16 or AWQ: the same model name, different numerics,
different outputs. Behaviour is characterised against one and served from the
other. For a component proposing values that reach a chart, "it behaved differently
in production" is not theoretical -- and concurrency differs too, so local latency
measurements predict little.

The deployment target itself is unresolved. Current deploy configs are local-only;
the OCI work exists on the `archive/oci-cloud-deploy` tag and was never merged. A
7-8B model needs a GPU shape or it runs on ARM CPU at seconds per response. That
question is this ticket's to answer, with numbers.

## Acceptance Criteria

- [ ] A vLLM client behind the same Protocol, selected by `LLM_PROVIDER`, with
      provider-specific structured-output handling behind the adapter boundary.
- [ ] TICK-062's corpus runs against both backends and the results are compared
      directly. **Any disagreement on a write is a blocker**, not a note: NFR-36 is
      zero wrong writes on the backend that serves patients.
- [ ] The same weights and quantisation are used on both sides where the runtimes
      allow it. Where they cannot be, the difference is recorded with what it
      changed in the results.
- [ ] Measured latency and throughput under realistic concurrency on the actual
      deployment shape, not on the development Mac. Recorded as numbers.
- [ ] The deployment shape is decided and written down: which OCI shape, whether it
      has a GPU, and what it costs. If the answer is that a free shape cannot serve
      this, that is the finding and it is recorded rather than worked around.
- [ ] Preflight and `verify-stack.sh` cover the deployed backend the way they cover
      the local one.

## Testing

The corpus against both backends with a written comparison, latency under
concurrency on the real shape, and a live end-to-end pass on the deployed
environment. Recorded under `evidence/TICK-066/`. CI must be green.

## Out of Scope

Building the OCI topology itself if it does not exist -- that is its own work, and
this ticket's finding may be that it must come first.
