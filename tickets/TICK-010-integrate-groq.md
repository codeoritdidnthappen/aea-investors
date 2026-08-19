---
id: TICK-010
title: "feat(llm): integrate approved Groq planning and streaming"
type: feature
epic: EPIC-03
priority: P1
estimate: M
depends_on: [TICK-005, TICK-009]
labels: [llm, groq, privacy]
source: [FR-18, FR-20, FR-29, NFR-2, NFR-5, NFR-26]
status: todo
remote_url: null
---

## Context

Groq `openai/gpt-oss-120b` is the pinned external model only after Zero Data Retention is verified and every call passes the local gate.

## Acceptance Criteria

- [ ] Startup blocks model traffic until Groq Zero Data Retention verification is recorded.
- [ ] The planning call validates schema output before any tool action.
- [ ] The final user-visible response streams only after authoritative tool results are known.
- [ ] Model failures produce no invented appointment fact or success claim.

## Testing

Use a captured fake client to verify model ID, approved fields, schema failures, streaming, and unavailable responses. CI must be green.

## Out of Scope

Alternative LLM providers or local inference.
