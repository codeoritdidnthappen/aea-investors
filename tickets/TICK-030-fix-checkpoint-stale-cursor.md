---
id: TICK-030
title: "task(onboarding): fold checkpoint_field's stale-cursor error into its documented contract"
type: task
epic: EPIC-06
priority: P2
estimate: XS
depends_on: [TICK-017]
labels: [onboarding]
source: [FR-30]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/55
builder_commit: 214b589
---

## Context

`OnboardingFlow.checkpoint_field()`'s `_checkpoint_node` catches
`AssessmentDraftValidationError` and `AssessmentDraftConflictError` from the draft
adapter's `update()` call, folding both into the documented `FieldCheckpointRejected`
contract — but not `AssessmentDraftNotFoundError`. This is the same class of gap
`complete()` had (fixed in PR #53, commit 1957252): if the cursor's `draft_uuid` no
longer resolves to an OpenEMR draft (deleted, expired, or a stale/corrupted cursor),
`checkpoint_field` raises the draft client's own `AssessmentDraftNotFoundError`
uncaught instead of the documented `FieldCheckpointRejected`, reaching any caller
that only handles the latter. Found by `/code-review` during TICK-017 (PR #53); not
fixed there per explicit direction to land the feature first and track this
separately.

## Acceptance Criteria

- [ ] `_checkpoint_node` (`ai_server/onboarding/flow.py`) catches
      `AssessmentDraftNotFoundError` and folds it into the same
      `{"error": [...]}` → `FieldCheckpointRejected` path already used for the
      other two draft-client exception types.
- [ ] A regression test mirrors
      `test_ac3_completion_with_a_stale_cursor_raises_the_documented_error_type`
      for `checkpoint_field`.
- [ ] Every existing onboarding-flow test still passes.

## Testing

Extend `ai_server/tests/test_onboarding_flow.py`. CI must be green.

## Out of Scope

Any other error-handling gap in the onboarding flow beyond this specific
exception type.
