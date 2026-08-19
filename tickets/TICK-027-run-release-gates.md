---
id: TICK-027
title: "task(local): verify privacy and local-demo readiness"
type: task
epic: EPIC-08
priority: P1
estimate: M
depends_on: [TICK-007, TICK-011, TICK-015, TICK-021, TICK-023, TICK-024, TICK-025, TICK-026]
labels: [release-gate, verification, privacy]
source: [NFR-1, NFR-8, NFR-18, NFR-20, NFR-21, NFR-22, NFR-23, NFR-24, NFR-26, NFR-28, NFR-29, NFR-34, NFR-35]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/28
---

## Context

The local demo cannot be represented as ready until all hard privacy, synthetic-data, OCR, artifact, local-topology, and browser gates have evidence.

## Acceptance Criteria

- [ ] A release checklist reconciles every P1 requirement and gate to executed evidence.
- [ ] Privacy golden corpus, OCR threshold, artifact exclusion, ZDR verification, local health, local TLS, desktop, and Android results are all passing.
- [ ] Exceptions identify the unmet requirement and prevent readiness approval.

## Testing

Independently re-run the release checklist against local artifacts and retain redacted evidence. CI must be green.

## Out of Scope

A production HIPAA-compliance claim.
