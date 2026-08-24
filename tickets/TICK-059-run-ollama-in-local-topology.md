---
id: TICK-059
title: "chore(deploy): run Ollama in the local topology and pin the model"
type: task
epic: EPIC-09
priority: P1
estimate: M
depends_on: [TICK-058]
labels: [deploy, llm]
source: [FR-33]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/124
builder_commit: 22da225
---
## Context

`docs/LOCAL_LLM_SPEC.md` D5, D7, D11. Development runs on Apple Silicon with
Ollama; deployment is Oracle Cloud with vLLM (TICK-067). This ticket stands up the
development half.

The model is a ~7-8B quantised instruct model (Llama 3.1 8B / Qwen2.5 7B /
Mistral 7B class). The exact model and quantisation are chosen by TICK-062's
benchmark, so this ticket must make the model a pinned, swappable setting rather
than a hard-coded name.

Pinning matters as much here as it does for the OpenEMR and MariaDB images
(NFR-15): an unpinned model tag would change the behaviour of a component that
proposes values for a medical record, silently, on a pull.

## Acceptance Criteria

- [ ] The model server runs as a service in the local topology and the AI server
      reaches it by service name, not by a host address that only works on one
      machine.
- [ ] The model is pinned by name **and** by digest or equivalent, so the same
      bytes are served after a rebuild. An unpinned or floating tag fails review.
- [ ] The model is available without a manual pull step, or the runbook states the
      step explicitly and `verify-stack.sh` fails when it has not been done. A stack
      that starts and then cannot answer is the failure mode this repo has already
      had twice.
- [ ] Model storage persists across a recreate, so a rebuild does not re-download
      several gigabytes.
- [ ] `docker compose up` still succeeds on a machine with no GPU. Slow is
      acceptable; failing to start is not.
- [ ] The preflight and `verify-stack.sh` cover the new service the way they cover
      the others: a missing model or an unreachable server is reported before a
      patient finds it.
- [ ] `AI_USAGE.md` records the pinned model and quantisation alongside the existing
      external-model and OCR entries (NFR-21).

## Testing

Bring the stack up from a clean checkout, assert the AI server reaches the model
server and gets a completion, then recreate and assert the model was not
re-downloaded. Record timings under `evidence/TICK-059/` -- they are the first real
data on whether D11's size assumption holds on this hardware. CI must be green.

## Out of Scope

The vLLM deployment (TICK-067). Choosing the exact model (TICK-062). Any change to
how turns are routed.
