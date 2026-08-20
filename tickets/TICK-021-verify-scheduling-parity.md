---
id: TICK-021
title: "task(scheduling): verify native policy parity"
type: task
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-031]
labels: [scheduling, verification]
source: [FR-28, NFR-11, NFR-12]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/22
---

## Context

Chat must match OpenEMR's allowed and rejected scheduling outcomes, not emulate them with independent logic.

## Dependency changed (2026-08-20)

Was `[TICK-020]`; TICK-020 split into TICK-031 (book + cancel, buildable) and a
narrowed TICK-020 (reschedule only, permanently blocked -- no OpenEMR service
method exists). This ticket now depends on TICK-031. The AC below already says
"cases exposed by OpenEMR" -- the parity matrix should document reschedule as not
exposed/not applicable rather than block on a capability that doesn't exist.

## Acceptance Criteria

- [ ] A parity matrix covers supported booking, cancellation, notice, and eligibility cases exposed by OpenEMR; reschedule is documented as not exposed/not applicable rather than verified, per the dependency note above (TICK-020 remains permanently blocked on it).
- [ ] Each case records the same native and chat result using synthetic data.
- [ ] Any discrepancy blocks release and identifies the authoritative OpenEMR response.

## Testing

Automate the parity matrix where APIs permit; run remaining browser/native checks against the local stack and record evidence. CI must be green.

## Out of Scope

Changing OpenEMR scheduling policy.
