---
id: TICK-011
title: "feat(operations): add safe health and observability controls"
type: feature
epic: EPIC-03
priority: P1
estimate: M
depends_on: [TICK-005, TICK-008, TICK-009]
labels: [operations, security]
source: [NFR-8, NFR-22]
status: todo
remote_url: null
---

## Context

The demo needs dependency health while guaranteeing that logs, traces, and analytics contain no patient, provider, token, prompt, or document data.

## Acceptance Criteria

- [ ] A health endpoint reports AI server, OpenEMR API, OCR, and external LLM reachability without sensitive configuration.
- [ ] Logging and tracing omit protected values on success and failure paths.
- [ ] Health behavior distinguishes unavailable dependencies without exposing credentials.

## Testing

Inject seeded sensitive values through each failure path and assert none appear in captured logs or health output. CI must be green.

## Out of Scope

Third-party analytics.
