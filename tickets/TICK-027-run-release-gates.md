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
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/28
blocked_reason: "Checklist run live 2026-08-20 -- see evidence/TICK-027/RELEASE_CHECKLIST.md. Seven of ten P1 gates pass with fresh evidence (privacy golden corpus, artifact exclusion, OCR threshold, ZDR, local health, local TLS, >=80% test coverage). Three exceptions remain, all inherited from their own tickets: TICK-024 (desktop E2E, blocked on TICK-039), TICK-025 (Android E2E, blocked on no emulator in this environment), TICK-026 (performance, deferred to avoid spending real money against a live API key without explicit authorization). Re-run once all three land/unblock."
---

## Context

The local demo cannot be represented as ready until all hard privacy, synthetic-data, OCR, artifact, local-topology, and browser gates have evidence.

**Attempted, blocked on inherited dependencies (2026-08-20):** ran the full
checklist live -- see `evidence/TICK-027/RELEASE_CHECKLIST.md`. Not a defect
in this ticket's own work; it correctly identifies and records the three
open exceptions per its own AC3, which is exactly why readiness is not
approved yet.

## Acceptance Criteria

- [x] A release checklist reconciles every P1 requirement and gate to executed evidence.
- [ ] Privacy golden corpus, OCR threshold, artifact exclusion, ZDR verification, local health, local TLS, desktop, and Android results are all passing. (Seven of ten pass; desktop, Android, and performance remain open exceptions -- see checklist.)
- [x] Exceptions identify the unmet requirement and prevent readiness approval.

## Testing

Independently re-run the release checklist against local artifacts and retain redacted evidence. CI must be green.

## Out of Scope

A production HIPAA-compliance claim.
