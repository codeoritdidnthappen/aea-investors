---
id: TICK-001
title: "spike(openemr): map required endpoints on a pinned release"
type: spike
epic: EPIC-01
priority: P1
estimate: L
depends_on: [TICK-005]
labels: [openemr, discovery]
source: [FR-3, FR-9, FR-10, FR-12, FR-13, FR-14, FR-17, FR-26, FR-27, FR-30, NFR-15, NFR-25]
status: done
builder_commit: 3b542b9
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/2
---

## Context

The API-only architecture requires an exact, release-specific endpoint map before the
affected adapter operation is implemented. Local foundation work may use documented
Standard REST APIs with synthetic data; missing coverage remains a documented blocker
for the affected operation and never permission for direct database access.

## Acceptance Criteria

- [ ] The then-current stable OpenEMR release is pinned with its source and image/reference.
- [ ] A versioned matrix records supported OAuth launch, appointments, availability, office hours, closures, reschedule, cancellation, demographics, assessment draft, and assessment completion operations.
- [ ] Each operation records endpoint, method, scope, request/response evidence, and any gap.
- [ ] Any gap is explicitly marked implementation-blocking with no database workaround.

## Testing

Run authenticated synthetic-patient probes against the pinned release where endpoints exist; preserve redacted request/response evidence. CI must be green.

## Out of Scope

Building production adapters or changing OpenEMR.
