---
id: TICK-026
title: "task(performance): measure chat and scheduling targets"
type: task
epic: EPIC-08
priority: P2
estimate: M
depends_on: [TICK-020, TICK-023]
labels: [performance, k6, verification]
source: [NFR-13, NFR-14]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/27
---

## Context

The documented should-level targets are less than 3.0 seconds p95 for chat/API and less than 1.0 second p95 for scheduling reads/writes at 20 virtual users for 60 seconds.

## Acceptance Criteria

- [ ] Reproducible load scripts execute the stated 20-user, 60-second scenario.
- [ ] The report separately measures chat/API and OpenEMR scheduling operations at p95.
- [ ] Results, environment, and any missed target are recorded without sensitive values.

## Testing

Run the load scripts against the OCI-equivalent deployed topology and archive the report. CI must be green.

## Out of Scope

Changing product scope to meet a target.
