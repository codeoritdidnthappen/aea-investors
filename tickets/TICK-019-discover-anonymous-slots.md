---
id: TICK-019
title: "feat(scheduling): expose genuine open slots as anonymous tokens"
type: feature
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-009, TICK-010, TICK-018]
labels: [scheduling, privacy, llm]
source: [FR-10, FR-11, FR-20, NFR-5, NFR-12]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/20
builder_commit: 7aef40e
---
## Context

The model may select an available option but never receives real OpenEMR identifiers; short-lived anonymous slot tokens bridge that boundary.

## Acceptance Criteria

- [ ] Only future, genuinely open OpenEMR slots are presented.
- [ ] Each candidate has a random, single-purpose, expiring token resolvable only inside the AI server.
- [ ] Outbound context contains only approved schedule fields and anonymous tokens.
- [ ] Expired or invalid tokens cannot cause an appointment action.

## Testing

Test slot filtering, token uniqueness/expiry, payload capture, and invalid-token handling against local synthetic data only. CI must be green.

## Out of Scope

AI scheduling rules or slot reservation outside OpenEMR.
