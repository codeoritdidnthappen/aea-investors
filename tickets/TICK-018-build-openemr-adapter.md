---
id: TICK-018
title: "feat(openemr): implement authoritative scheduling adapter"
type: feature
epic: EPIC-07
priority: P1
estimate: L
depends_on: [TICK-001, TICK-008]
labels: [openemr, scheduling]
source: [FR-9, FR-10, FR-15, FR-17, FR-20, FR-28, NFR-12, NFR-25]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/19
builder_commit: 8d731fb
---
## Context

The adapter turns the endpoint map into user-scoped reads and writes against the pinned, disposable local OpenEMR stack while keeping OpenEMR authoritative for all facts and policy. Unsupported operations remain explicit local integration gaps; no database fallback is permitted.

## Acceptance Criteria

- [ ] The adapter reads active appointments only through mapped endpoints; unavailable availability, office-hours, and closure operations fail explicitly with no fallback until a mapped endpoint exists.
- [ ] Results include explicit timezones and remain correct across DST.
- [ ] Cancelled appointments are omitted from patient chat results but are not deleted.
- [ ] The adapter adds no booking, eligibility, notice, or scheduling default.

## Testing

Integration-test supported operations, cancellation filtering, and DST fixtures against a pinned synthetic OpenEMR Docker stack running locally. CI must be green.

## Out of Scope

Model prompting or a parallel schedule database.
