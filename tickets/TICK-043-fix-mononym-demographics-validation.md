---
id: TICK-043
title: "bug(onboarding): a confirmed mononym (empty family name) fails OpenEMR's own patient validator"
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

Found and live-confirmed during TICK-042's code review (2026-08-20), not a
regression from that ticket: this codebase's own `confirm_identity()`
(`ai_server/openemr/demographics.py`) and `PatientDemographicsUpdateService`
(`openemr_modules/aeai-portal-chat`) both explicitly allow a confirmed
mononym -- `family_name`/`lname` may be an empty string, matching a patient
who has only one legal name. But OpenEMR's own `PatientService::update()`
(`src/Services/PatientService.php:307`) runs `PatientValidator`'s
`DATABASE_UPDATE_CONTEXT`, which applies `lengthBetween(2, 255)` to `lname`
regardless of the `required(false)` update-context override, and
`particle/validator`'s `NotEmpty` rule rejects an empty string outright.

Live-confirmed directly against the running container:

```
$svc->update($uuid, ["fname" => "Cher", "lname" => "", "DOB" => "1990-01-01", "street" => "1 Test St"]);
// isValid: false
// validationMessages: {"lname":{"NotEmpty::EMPTY_VALUE":"Last Name must not be empty"}}
```

This is not something TICK-042 introduced: the old (unreachable)
`PUT /api/patient/:puuid` Standard API route called the exact same
`PatientService::update()` and would have hit the identical rejection had it
ever been reachable for a patient token. TICK-042 only made the *common*
case (non-empty family name) actually work end to end; a confirmed mononym
still cannot complete onboarding.

## Acceptance Criteria

- [ ] A patient who confirms a mononym (empty family name) during onboarding
      completes successfully -- either OpenEMR accepts an empty `lname`
      through some documented mechanism (verify: does the Standard API's own
      `POST /api/patient` insert path handle this differently, or does every
      OpenEMR patient require a non-empty last name by design?), or this
      product deliberately does not support mononym patients and the
      onboarding flow's own field validation (`ai_server/onboarding/fields.py`)
      is changed to reject an empty family name before ever reaching OpenEMR,
      with a clear, honest message instead of a generic write failure.
- [ ] Whichever direction is chosen, `confirm_identity()`'s docstring and the
      existing test suite (`test_ac2_a_confirmed_mononym_has_no_fabricated_family_name`,
      `test_ac3_completion_writes_demographics_and_finalizes_the_native_assessment`-adjacent
      mononym tests) are updated to match reality, not aspiration.

## Testing

Live verification against the local Docker topology (`PatientService::update()`
called directly with an empty `lname`, as done for this ticket's own
investigation) plus the existing/updated pytest suite.

## Out of Scope

Any other OpenEMR patient-validation field beyond `lname`.
