---
id: TICK-003
title: "spike(product): define the intake and supportive-content contract"
type: spike
epic: EPIC-01
priority: P1
estimate: M
depends_on: []
labels: [product, onboarding, discovery]
source: [FR-5, FR-8, FR-27]
status: todo
remote_url: null
---

## Context

The selected onboarding brief and mapped supportive content are referenced but absent from the repository. The conversational flow cannot be implemented or tested until their contract is explicit.

## Acceptance Criteria

- [ ] A versioned product artifact defines each assessment field, validation, ordering, draft semantics, and completion rule.
- [ ] It maps long pause, upload failure, and distress intent to approved supportive content and defines the no-trigger behavior.
- [ ] It identifies which fields are deterministic/local versus model-mediated without introducing clinical advice.
- [ ] Product approval is recorded before implementation begins.

## Testing

Review the artifact against FR-5, FR-8, and FR-27; create fixture cases for every field and trigger. CI must be green.

## Out of Scope

Designing a staff workflow or adding clinical treatment guidance.
