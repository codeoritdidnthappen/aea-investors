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
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/18
builder_commit: b193665
---
## Context

The approved product contract drives an assessment that keeps drafts and completed records in OpenEMR, while supportive content appears only at defined friction triggers.

## Platform gap resolved (2026-08-20)

This ticket was `status: blocked` because OpenEMR v8.3.0 had no native
draft-checkpoint or completion endpoint (`evidence/TICK-001/ENDPOINT_MATRIX.md`).
That gap is now closed: `openemr_modules/aeai-portal-chat` registers
`POST/GET/PUT /portal/patient/assessment[/:auuid]` through OpenEMR's own
`RestApiCreateEvent`/`RestApiScopeEvent` module-extension events — no core OpenEMR
file modified, the pinned `openemr/openemr:8.3.0` image untouched. Proven against
the live local stack with a real `authorization_code`+PKCE patient token, including
a cross-patient negative test (patient B reading/writing patient A's draft both
404), the same discipline `evidence/TICK-028/BINDING_MATRIX.md` used. Full record:
`evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md`; the resolved endpoint-matrix rows
are in `evidence/TICK-001/ENDPOINT_MATRIX.md`.

This closes only the OpenEMR-side gap named in this ticket's `blocked_reason`. It
does not implement this ticket's guided-conversation flow: the LangGraph state
machine, friction-trigger supportive content (long pause / upload failure /
distress intent), and field-by-field conversational collection described in
`ONBOARDING_CONTRACT.md` are separate, substantial AI-server work not started here.
`status` stays `todo` rather than `done` for that reason.

## Acceptance Criteria

- [ ] The flow collects and validates every approved assessment field and produces the approved structured record.
- [ ] Draft changes checkpoint through the mapped OpenEMR endpoint and reload after AI-server restart.
- [ ] Completion persists the native assessment through OpenEMR and stores no separate durable patient record.
- [ ] Long pause, upload failure, and distress intent show exactly their approved supportive content; no trigger produces none.
- [ ] Sensitive patient content is not sent to the external model.

## Testing

Run contract fixtures, restart recovery, trigger/no-trigger cases, and local synthetic OpenEMR persistence integration tests. CI must be green.

## Out of Scope

Clinical treatment advice or an AI-owned assessment store.
