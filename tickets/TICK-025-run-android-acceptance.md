---
id: TICK-025
title: "task(verification): validate approved Android Chrome behavior"
type: task
epic: EPIC-08
priority: P1
estimate: M
depends_on: [TICK-004, TICK-013, TICK-014, TICK-031, TICK-023]
labels: [android, chrome, verification]
source: [NFR-19, NFR-35]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/26
blocked_reason: "evidence/TICK-004-android-chrome-acceptance-draft.md's A1 matrix requires a real Android Emulator target (API 35+, Pixel-class, 360-432 CSS px viewport). No Android emulator is available in this execution environment (confirmed unavailable even in the Claude Desktop app's own feature flags). Environment gap, not a product/code blocker -- resolve by running this ticket in an environment with emulator access."
---

## Context

Android Chrome is in scope with explicitly approved lower-priority degradation only.

**Dependency changed (2026-08-20):** was `TICK-020`; split into TICK-031 (book +
cancel, buildable) and a narrowed TICK-020 (reschedule only, permanently
blocked). Separately, AND-SCHEDULE-01's "reschedule an existing appointment" step
(evidence/TICK-004-android-chrome-acceptance-draft.md) can never pass regardless of
environment access, since TICK-020 documents reschedule as permanently blocked --
that step will need updating to book/cancel-only when this ticket is eventually run.

## Acceptance Criteria

- [ ] Every required flow and non-negotiable accessibility behavior from TICK-004 passes on each supported Android Chrome target.
- [ ] Each permitted degradation is observed, documented, and remains within the approved contract.
- [ ] No iOS or other-browser result is represented as v1 coverage.

## Testing

Execute the approved Android device/version matrix against the local topology with retained screenshots and network evidence. CI must be green.

## Out of Scope

Unapproved mobile refinements.
