---
id: TICK-004
title: "spike(mobile): define Android Chrome acceptance"
type: spike
epic: EPIC-01
priority: P1
estimate: S
depends_on: []
labels: [mobile, accessibility, discovery]
source: [NFR-19, NFR-35]
status: blocked
blocked_reason: "The drafted Android matrix, degradation policy, accessibility rules, and verification cases require product approval."
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/5
---

## Context

Android Chrome is the sole mobile target and is lower priority than desktop. Its acceptable functional, visual, accessibility, and performance degradation must be explicit rather than assumed.

## Acceptance Criteria

- [ ] The supported Android Chrome version/device matrix is recorded.
- [ ] Required mobile flows, allowed degradation, and non-negotiable accessibility behavior are approved.
- [ ] The resulting criteria define observable pass/fail checks for iframe launch, cookie session, streaming, upload, and scheduling.

## Testing

Review the matrix with product and translate every required flow into Android verification cases. CI must be green.

## Out of Scope

iOS Chrome or other browser families.
