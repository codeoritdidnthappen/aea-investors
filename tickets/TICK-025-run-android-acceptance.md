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
blocked_reason: "evidence/TICK-004-android-chrome-acceptance-draft.md's A1 matrix requires a real Android Emulator target (API 35+, Pixel-class, 360-432 CSS px viewport). No Android emulator is available in this execution environment (confirmed unavailable even in the Claude Desktop app's own feature flags). Environment gap, not a product/code blocker. Per explicit user direction 2026-08-21, marked out of scope for this environment/session rather than left as an indefinitely-pending exception -- see TICK-027's own re-scoped release checklist. Resume as a future ticket once Android emulator or device access becomes available; do not attempt in this environment in the meantime."
---

## Context

Android Chrome is in scope with explicitly approved lower-priority degradation only.

**Dependency changed (2026-08-20):** was `TICK-020`; split into TICK-031 (book +
cancel, buildable) and a narrowed TICK-020 (reschedule only, permanently
blocked). Separately, AND-SCHEDULE-01's "reschedule an existing appointment" step
(evidence/TICK-004-android-chrome-acceptance-draft.md) can never pass regardless of
environment access, since TICK-020 documents reschedule as permanently blocked --
that step will need updating to book/cancel-only when this ticket is eventually run.

**Note (2026-08-20):** TICK-020 itself has since landed (booking/cancel/reschedule
all built and verified on desktop) -- the note above about AND-SCHEDULE-01 needing
an update to book/cancel-only still applies whenever this ticket is next run, since
reschedule specifically remains permanently unavailable regardless of platform.

**Marked out of scope for this environment (2026-08-21):** per explicit user
direction, this ticket is not being force-attempted or left as an
indefinitely-open exception. It genuinely cannot be run here -- there is no
Android emulator or device reachable from this session, and no code change
can substitute for one. Whoever picks this up next should treat it as a
fresh attempt requiring real Android emulator or device access (e.g. a
local Android Studio emulator, a physical device over ADB, or a remote
device farm); nothing about the product itself is known to be broken on
Android, this is purely an unexercised gap.

## Acceptance Criteria

- [ ] Every required flow and non-negotiable accessibility behavior from TICK-004 passes on each supported Android Chrome target.
- [ ] Each permitted degradation is observed, documented, and remains within the approved contract.
- [ ] No iOS or other-browser result is represented as v1 coverage.

## Testing

Execute the approved Android device/version matrix against the local topology with retained screenshots and network evidence. CI must be green.

## Out of Scope

Unapproved mobile refinements.
