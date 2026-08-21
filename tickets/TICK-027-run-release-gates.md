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
blocked_reason: "Checklist re-run live 2026-08-20 (4th pass) -- see evidence/TICK-027/RELEASE_CHECKLIST.md. Nine of ten P1 gates now pass with fresh evidence -- desktop Chrome (TICK-024) closed out this pass now that TICK-044 landed and was live-verified (OCR confirmation was the last open item). Two exceptions remain, both environment/authorization gaps, not product defects: TICK-025 (Android E2E, no emulator available in this environment) and TICK-026 (performance, requires explicit user authorization to spend against a live API key, not yet requested)."
---

## Context

The local demo cannot be represented as ready until all hard privacy, synthetic-data, OCR, artifact, local-topology, and browser gates have evidence.

**Attempted, blocked on inherited dependencies (2026-08-20):** ran the full
checklist live -- see `evidence/TICK-027/RELEASE_CHECKLIST.md`. Not a defect
in this ticket's own work; it correctly identifies and records the three
open exceptions per its own AC3, which is exactly why readiness is not
approved yet.

**Re-run a third time (2026-08-20):** now that TICK-020/038/039/040/041/042
have all landed, re-checked every gate live. Reschedule (TICK-020) is no
longer a permanent exception -- it landed and was verified. Desktop E2E
(TICK-024) narrowed from three open findings to a single remaining gap: OCR
confirmation has no live path to test at all (a missing integration, filed
as TICK-044), not a bug. Android (TICK-025) and performance (TICK-026)
remain unchanged, for the same reasons already on record. See
`evidence/TICK-027/RELEASE_CHECKLIST.md`.

**Re-run a fourth time (2026-08-20):** TICK-044 landed and was live-verified
through the real chat UI. Desktop Chrome (TICK-024) now fully passes --
`status: done`. Nine of ten P1 gates pass; only Android (environment gap)
and performance (needs explicit spend authorization) remain open, neither a
product defect.

## Acceptance Criteria

- [x] A release checklist reconciles every P1 requirement and gate to executed evidence.
- [ ] Privacy golden corpus, OCR threshold, artifact exclusion, ZDR verification, local health, local TLS, desktop, and Android results are all passing. (Nine of ten pass; only Android remains an open exception among the eight explicitly named here -- performance is tracked as a ninth gate outside this AC's own list and also remains open -- see checklist.)
- [x] Exceptions identify the unmet requirement and prevent readiness approval.

## Testing

Independently re-run the release checklist against local artifacts and retain redacted evidence. CI must be green.

## Out of Scope

A production HIPAA-compliance claim.
