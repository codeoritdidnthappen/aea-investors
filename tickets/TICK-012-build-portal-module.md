---
id: TICK-012
title: "feat(portal): embed the authenticated chat iframe"
type: feature
epic: EPIC-04
priority: P1
estimate: M
depends_on: [TICK-002, TICK-008]
labels: [openemr, portal, frontend]
source: [FR-1, FR-2, FR-3, FR-4, NFR-6, NFR-7]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/13
---

## Context

The selected supported patient-portal hook launches the chat without navigating away and maintains browser isolation from OpenEMR APIs.

## Acceptance Criteria

- [ ] Only an authenticated patient can see and launch the portal entry.
- [ ] The entry renders the chat inside the OpenEMR portal using the selected hook.
- [ ] The iframe communicates only with the AI server; browser network capture shows no OpenEMR API request from it.

## Testing

Run synthetic-patient login/launch/logout browser tests and inspect network requests. CI must be green.

## Out of Scope

Staff, provider, office, or admin AI chat.
