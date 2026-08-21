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
blocked_reason: "Re-attempted live 2026-08-21 per explicit user re-authorization: ran a cost-contained trial (5 VU/30s, not the full 20 VU/60s AC1 names) covering both chat/API and OpenEMR scheduling read/write. All three measured p95s passed comfortably (chat/API 409ms vs <3.0s target; scheduling read 29.5ms and write 52.94ms vs <1.0s target) -- see evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md, including a real methodology finding (concurrent read+write against one patient inflates read latency as a test artifact, corrected by re-running reads in isolation). Two reproducible scripts now exist (scripts/k6_performance_test.js, scripts/load_test_chat_browser.js). AC1 still requires either explicit authorization to run the full 20-VU/60s scenario against the local key, or re-scoping to production."
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

**Re-attempted at reduced, explicitly authorized scale (2026-08-21):** asked
before running anything; user authorized a 5-VU/30s trial (not yet the full
20-VU/60s AC1 names) covering both scenarios together. Real OAuth-provisioned
credentials (a genuine consented login, not a mock), real requests against
the live local deployment. All three p95s passed with wide margin. See
`evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md` for full results,
including a real test-methodology finding (concurrent read+write against one
patient's appointment history inflates read latency as an artifact of the
test design, not a genuine OpenEMR performance issue -- caught, diagnosed,
and corrected within this same pass). AC1 remains open: it names the full
20-VU/60s scenario specifically, which this trial was explicitly scoped
smaller than.

## Acceptance Criteria

- [ ] Reproducible load scripts execute the stated 20-user, 60-second scenario. (Scripts exist and are proven correct at 5 VU/30s; the full 20/60 scale itself has not yet run -- needs further authorization or production re-scoping.)
- [x] The report separately measures chat/API and OpenEMR scheduling operations at p95.
- [x] Results, environment, and any missed target are recorded without sensitive values.

## Testing

Run the load scripts against the local Docker topology and archive the local report. CI must be green.

## Out of Scope

Changing product scope to meet a target.
