---
id: TICK-003
title: "spike(product): define the intake and supportive-content contract"
type: spike
epic: EPIC-01
priority: P1
estimate: M
depends_on: [TICK-005]
labels: [product, onboarding, discovery]
source: [FR-5, FR-8, FR-27]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/4
---

## Context

The selected onboarding brief and mapped supportive content are referenced but absent from the repository. The conversational flow cannot be implemented or tested until their contract is explicit.

## Acceptance Criteria

- [x] A versioned product artifact defines each assessment field, validation, ordering, draft semantics, and completion rule.
- [x] It maps long pause, upload failure, and distress intent to approved supportive content and defines the no-trigger behavior.
- [x] It identifies which fields are deterministic/local versus model-mediated without introducing clinical advice.
- [x] Product approval is recorded before implementation begins.

## Testing

Review the artifact against FR-5, FR-8, and FR-27; create fixture cases for every field and trigger. CI must be green.

## Verification

`ONBOARDING_CONTRACT.md` contains the field and trigger fixture cases and was approved
by the product owner on 2026-08-18. TICK-003 runs after TICK-005 establishes the
required CI gate.

## Out of Scope

Designing a staff workflow or adding clinical treatment guidance.
