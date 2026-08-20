---
id: TICK-026
title: "task(performance): measure chat and scheduling targets"
type: task
epic: EPIC-08
priority: P2
estimate: M
depends_on: [TICK-031, TICK-023]
labels: [performance, k6, verification]
source: [NFR-13, NFR-14]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/27
blocked_reason: "Deferred 2026-08-20 per explicit user direction: the local ai-server has a real, live Groq API key (not a mock); a genuine 20-VU/60s load test against /api/chat would spend real money hitting it several hundred times. Run the full load test later against the live/production site instead of locally. Not an implementation blocker -- unblock by re-scoping to production or by explicit re-authorization to spend against the local key."
---

## Context

The documented should-level targets are less than 3.0 seconds p95 for chat/API and less than 1.0 second p95 for scheduling reads/writes at 20 virtual users for 60 seconds.

**Dependency changed (2026-08-20):** was `TICK-020`; split into TICK-031 (book +
cancel, buildable) and a narrowed TICK-020 (reschedule only, permanently
blocked). "Scheduling reads/writes" here means book/cancel.

**Deferred (2026-08-20):** the local ai-server has a real, live Groq API key
configured (not a mock) -- a genuine 20-VU/60s load test against `/api/chat`
would fire several hundred real, metered LLM calls and spend real money.
Explicit user direction: defer this ticket entirely and run the full load test
later against the live/production site instead of the local demo stack. Not
attempted here.

## Acceptance Criteria

- [ ] Reproducible load scripts execute the stated 20-user, 60-second scenario.
- [ ] The report separately measures chat/API and OpenEMR scheduling operations at p95.
- [ ] Results, environment, and any missed target are recorded without sensitive values.

## Testing

Run the load scripts against the local Docker topology and archive the local report. CI must be green.

## Out of Scope

Changing product scope to meet a target.
