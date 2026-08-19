---
id: TICK-017
title: "feat(onboarding): guide assessment and checkpoint native drafts"
type: feature
epic: EPIC-06
priority: P1
estimate: L
depends_on: [TICK-001, TICK-003, TICK-009, TICK-010]
labels: [onboarding, langgraph, openemr]
source: [FR-5, FR-8, FR-27, FR-30, NFR-2, NFR-3, NFR-4, NFR-33]
status: todo
remote_url: null
---

## Context

The approved product contract drives an assessment that keeps drafts and completed records in OpenEMR, while supportive content appears only at defined friction triggers.

## Acceptance Criteria

- [ ] The flow collects and validates every approved assessment field and produces the approved structured record.
- [ ] Draft changes checkpoint through the mapped OpenEMR endpoint and reload after AI-server restart.
- [ ] Completion persists the native assessment through OpenEMR and stores no separate durable patient record.
- [ ] Long pause, upload failure, and distress intent show exactly their approved supportive content; no trigger produces none.
- [ ] Sensitive patient content is not sent to the external model.

## Testing

Run contract fixtures, restart recovery, trigger/no-trigger cases, and OpenEMR persistence integration tests. CI must be green.

## Out of Scope

Clinical treatment advice or an AI-owned assessment store.
