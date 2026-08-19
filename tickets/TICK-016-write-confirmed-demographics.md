---
id: TICK-016
title: "feat(demographics): persist only confirmed identity fields"
type: feature
epic: EPIC-05
priority: P1
estimate: M
depends_on: [TICK-001, TICK-014]
labels: [openemr, ocr, demographics]
source: [FR-6, FR-26, FR-17, NFR-25]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/17
---

## Context

Only a patient's confirmed or corrected name, date of birth, and address may change the logged-in OpenEMR record through a discovered endpoint.

## Acceptance Criteria

- [ ] Every extracted field is shown for confirmation or correction before a write is available.
- [ ] The adapter writes only confirmed name, date of birth, and address to the logged-in patient through TICK-001's endpoint.
- [ ] Unconfirmed, partial, revoked, and failed OCR values never mutate OpenEMR.

## Testing

Integration-test confirmed update and each no-write path against synthetic OpenEMR. CI must be green.

## Out of Scope

Any other demographic fields or direct MariaDB access.
