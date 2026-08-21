---
id: TICK-043
title: "bug(onboarding): a confirmed mononym (empty family name) can never even be entered"
type: task
epic: EPIC-07
priority: P2
estimate: S
depends_on: [TICK-042]
labels: [onboarding, openemr]
source: [FR-6, FR-17, FR-26]
status: todo
remote_url:
---
## Context

Found during TICK-042's code review (2026-08-20). **Correction from this
ticket's original text**: the failure does not occur at OpenEMR's
`PatientValidator` as first diagnosed -- a second review pass caught that
diagnosis was wrong. The real, earlier failure point is this codebase's own
local field validation, which rejects an empty family name before the value
is ever held for confirmation, let alone written to OpenEMR.

`ai_server/onboarding/fields.py:165-172` (`validate_text_name`, used for
both `given_name` and `family_name` via `validate_field()`):

```python
def validate_text_name(value: object, *, label: str) -> str:
    """Validate a legal given/family name: 1-100 non-whitespace characters."""
    if not isinstance(value, str):
        raise FieldValidationError([f"{label} must be text"])
    stripped = value.strip()
    if not stripped or len(stripped) > _MAX_TEXT_FIELD_LENGTH:
        raise FieldValidationError([f"{label} must be 1-100 non-whitespace characters"])
    return stripped
```

This runs at the very first opportunity: when a patient answers "What is
your legal family (last) name?" with an empty string,
`OnboardingChatService._handle_default`
(`ai_server/app/onboarding_chat.py:279-284`) calls `validate_field` directly
and rejects it with "family_name must be 1-100 non-whitespace characters"
before `state.identity["family_name"]` is ever set. The same check runs
again inside `OnboardingFlow.complete()` (`ai_server/onboarding/flow.py:203`)
as a second, defensive pass. A mononym patient can never even progress past
this one question -- they never reach the review step, let alone the
OpenEMR write.

This directly contradicts `confirm_identity()`'s own docstring
(`ai_server/openemr/demographics.py`), which explicitly documents and tests
(`test_ac2_a_confirmed_mononym_has_no_fabricated_family_name`) an empty
`family_name` as a supported, deliberate case -- a mononym. That downstream
support is unreachable dead code today: nothing upstream can ever produce an
empty `family_name` value for it to receive.

(For completeness: OpenEMR's own `PatientService::update()` -- verified live
during the original investigation -- also independently rejects an empty
`lname` via `PatientValidator`'s `NotEmpty` rule, so even if the local
validation gap above were fixed alone, the write would still fail at
OpenEMR. Both layers need to agree before a mononym can complete
onboarding.)

## Acceptance Criteria

- [ ] Decide the product direction: either (a) mononym patients are a
      supported case, and `validate_text_name` is changed to allow an empty
      `family_name` specifically (not `given_name`, which OpenEMR requires
      unconditionally) -- in which case OpenEMR's own `PatientValidator`
      rejection (see above) must also be resolved before completion can
      succeed, likely requiring a fallback/placeholder value or a different
      OpenEMR field; or (b) this product does not support mononym patients
      today, and `confirm_identity()`'s docstring/tests and
      `PatientDemographicsUpdateService`'s mononym allowance are corrected
      to match reality (require non-empty `lname` throughout) instead of
      documenting a code path nothing can ever reach.
- [ ] Whichever direction is chosen, the relevant validators, docstrings,
      and tests (`ai_server/onboarding/fields.py`,
      `ai_server/openemr/demographics.py`,
      `openemr_modules/aeai-portal-chat/src/Service/PatientDemographicsUpdateService.php`,
      and their test suites) are internally consistent -- no layer may
      document or test support for a case another layer unconditionally
      rejects.

## Testing

A test that exercises the actual onboarding-chat turn sequence (answering
`family_name` with an empty string), not just `confirm_identity()` in
isolation -- the existing mononym tests only cover the latter and would not
have caught this.

## Out of Scope

Any other OpenEMR patient-validation field beyond `lname`/`family_name`.
