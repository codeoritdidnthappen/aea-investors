---
id: TICK-021
title: "task(scheduling): verify native policy parity"
type: task
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-020]
labels: [scheduling, verification]
source: [FR-28, NFR-11, NFR-12]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/22
---

## Context

Chat must match OpenEMR's allowed and rejected scheduling outcomes, not emulate them with independent logic.

## Acceptance Criteria

- [ ] A parity matrix covers supported booking, reschedule, cancellation, notice, and eligibility cases exposed by OpenEMR.
- [ ] Each case records the same native and chat result using synthetic data.
- [ ] Any discrepancy blocks release and identifies the authoritative OpenEMR response.

## Testing

Automate the parity matrix where APIs permit; run remaining browser/native checks against the local stack and record evidence. CI must be green.

## Out of Scope

Changing OpenEMR scheduling policy.
