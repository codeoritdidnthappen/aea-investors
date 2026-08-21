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
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/28
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

**Re-run a fifth time (2026-08-21):** performance (TICK-026) closed out
this pass. Explicit user authorization for a cost-contained trial (5 VU/30s,
not the originally-documented 20 VU/60s), followed by explicit direction to
accept that reduced scale as the ticket's own completed scenario. All three
measured p95s passed with wide margin. Nine of ten gates now pass; only
Android (environment gap, no emulator available) remains open.

**Closed, Android explicitly re-scoped out (2026-08-21):** per explicit user
direction, TICK-025 (Android E2E) is marked out of scope for this
environment/session -- there is no Android emulator or device reachable
here, and no code change can substitute for one. Rather than leave this
ticket indefinitely blocked on a gap nothing here can close, AC2 is
re-scoped to explicitly exclude the Android result (matching TICK-026's own
precedent this same day), and the ticket is closed on that basis. Android
E2E remains a real, tracked gap -- TICK-025 stays `status: blocked`, noted
as resumable in a future ticket once emulator/device access exists -- it is
just no longer this ticket's own blocker.

## Acceptance Criteria

- [x] A release checklist reconciles every P1 requirement and gate to executed evidence.
- [x] Privacy golden corpus, OCR threshold, artifact exclusion, ZDR verification, local health, local TLS, desktop, and performance results are all passing. (Re-scoped 2026-08-21, explicit user direction: Android is excluded from this AC and tracked instead as TICK-025's own separate, environment-gapped ticket -- see checklist and Context above.)
- [x] Exceptions identify the unmet requirement and prevent readiness approval. (Android's exclusion itself is the recorded, explicitly-scoped exception -- not glossed over, just moved to its own ticket rather than blocking this one indefinitely.)

## Testing

Independently re-run the release checklist against local artifacts and retain redacted evidence. CI must be green.

## Out of Scope

A production HIPAA-compliance claim.
