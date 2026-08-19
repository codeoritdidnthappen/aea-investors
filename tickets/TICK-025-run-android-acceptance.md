---
id: TICK-025
title: "task(verification): validate approved Android Chrome behavior"
type: task
epic: EPIC-08
priority: P1
estimate: M
depends_on: [TICK-004, TICK-013, TICK-014, TICK-020, TICK-023]
labels: [android, chrome, verification]
source: [NFR-19, NFR-35]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/26
---

## Context

Android Chrome is in scope with explicitly approved lower-priority degradation only.

## Acceptance Criteria

- [ ] Every required flow and non-negotiable accessibility behavior from TICK-004 passes on each supported Android Chrome target.
- [ ] Each permitted degradation is observed, documented, and remains within the approved contract.
- [ ] No iOS or other-browser result is represented as v1 coverage.

## Testing

Execute the approved Android device/version matrix against the local topology with retained screenshots and network evidence. CI must be green.

## Out of Scope

Unapproved mobile refinements.
