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
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/27
---

## Context

The documented should-level targets are less than 3.0 seconds p95 for chat/API and less than 1.0 second p95 for scheduling reads/writes at 20 virtual users for 60 seconds (NFR-13/NFR-14, `PRD.md`).

**Scope note:** this ticket's own AC1 is closed against a deliberately
reduced, explicitly authorized load scale -- **5 virtual users for 30
seconds**, not the 20/60 figure NFR-13/NFR-14 document -- per explicit user
direction 2026-08-21 (see below). `PRD.md`'s NFR-13/NFR-14 text itself was
not changed; if the full 20-VU/60s scale is needed later (e.g. before a
production release claim), re-run `scripts/k6_performance_test.js` and
`scripts/load_test_chat_browser.js` with `VUS=20`/`DURATION=60s` -- both
scripts already support it unchanged.

**Dependency changed (2026-08-20):** was `TICK-020`; split into TICK-031 (book +
cancel, buildable) and a narrowed TICK-020 (reschedule only, permanently
blocked). "Scheduling reads/writes" here means book/cancel.

**Deferred (2026-08-20):** the local ai-server has a real, live Groq API key
configured (not a mock) -- a genuine 20-VU/60s load test against `/api/chat`
would fire several hundred real, metered LLM calls and spend real money.
Explicit user direction: defer this ticket entirely and run the full load test
later against the live/production site instead of the local demo stack. Not
attempted here.

**Re-attempted and closed at reduced, explicitly authorized scale
(2026-08-21):** asked before running anything; user authorized a 5-VU/30s
trial covering both scenarios together, then explicitly directed that this
reduced scale be accepted as the ticket's own completed scenario rather
than left open pending the full 20/60 figure. Real OAuth-provisioned
credentials (a genuine consented login, not a mock), real requests against
the live local deployment. All three p95s passed with wide margin:

| Scenario | Target | p95 (5 VU / 30s) |
|---|---|---|
| Chat/API (`POST /api/chat`, real Groq calls) | <3.0s (NFR-13) | 409ms |
| OpenEMR scheduling read | <1.0s (NFR-14) | 29.5ms |
| OpenEMR scheduling write (book + cancel) | <1.0s (NFR-14) | 52.94ms |

Full results in `evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md`,
including a real test-methodology finding along the way: running the read
and write scenarios concurrently against one synthetic patient let the
write scenario's ~1,771 book/cancel iterations inflate that patient's FHIR
appointment bundle mid-run, pushing measured read p95 to a misleading
1.68s. Caught, diagnosed, the synthetic rows cleaned up, and reads
re-measured in isolation for the corrected 29.5ms figure reported above --
not a genuine OpenEMR performance problem, a self-inflicted test-design
confound.

## Acceptance Criteria

- [x] Reproducible load scripts execute the stated user/duration scenario -- re-scoped 2026-08-21 to 5 VUs for 30 seconds (see Scope note above), not the originally-stated 20 users/60 seconds.
- [x] The report separately measures chat/API and OpenEMR scheduling operations at p95.
- [x] Results, environment, and any missed target are recorded without sensitive values.

## Testing

Ran the load scripts against the local Docker topology and archived the local report (`evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md`). CI is unaffected -- these are standalone k6/browser scripts under `scripts/`, not part of the pytest suite.

## Out of Scope

Changing product scope to meet a target.
