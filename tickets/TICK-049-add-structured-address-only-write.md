---
id: TICK-049
title: "feat(demographics): support an address-only write with structured address columns"
type: feature
epic: EPIC-05
priority: P1
estimate: M
depends_on: [TICK-016, TICK-042, TICK-043]
labels: [demographics, openemr, backend]
source: [FR-6, FR-17, FR-26, NFR-25]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/99
builder_commit: 5f0eb8b
---
## Context

Groundwork for TICK-050 (updating an address from the chat). Two things in
today's demographics write path make an address-only update impossible.

### 1. The write requires all four identity fields

`ConfirmedIdentity` (`ai_server/openemr/demographics.py:42-60`) holds
`given_name`, `family_name`, `date_of_birth`, and `address`, and
`confirm_identity()` (`:63-92`) raises `IdentityNotConfirmedError` if any
one of them is `None` or blank (`:83-85`). The PHP side agrees:
`PatientDemographicsUpdateService.php:35` declares
`REQUIRED_STRING_FIELDS = ['fname', 'lname', 'DOB', 'street']` and
**silently drops** anything not in that list (`validatedFields`, `:62-81`).

So a patient who only wants to change their address would have to retype
their name and date of birth, and any field not among those four cannot be
written at all.

### 2. The address is flattened into one column

`_format_address()` (`ai_server/onboarding/flow.py:299-306`) collapses a
structured `Address` into a single string, and the adapter sends it as
`street` (`demographics.py:115-120`):

```python
body = {
    "fname": identity.given_name,
    "lname": identity.family_name,
    "DOB": identity.date_of_birth,
    "street": identity.address,
}
```

The result is `street = "42 Oak St, Springfield, IL 62704"` with OpenEMR's
own `city`, `state`, and `postal_code` columns left empty. The validated
structure already exists locally — `Address`
(`ai_server/onboarding/fields.py:155-162`) carries `street1`, `street2`,
`city`, `state`, `zip_code`, and `validate_address` (`:194-232`) enforces
a real state code and ZIP pattern — it is simply discarded at the wire
boundary. TICK-042 explicitly deferred this (its Out of Scope, lines
99-101); this ticket closes that gap, since TICK-050 shows the patient a
parsed address and it should match what is actually stored.

## Acceptance Criteria

- [ ] A partial, address-only demographics write is possible: the patient's
      address can be updated without supplying or re-confirming
      `given_name`, `family_name`, or `date_of_birth`, and doing so must
      not blank out or overwrite those existing values in OpenEMR — verified
      by reading the record back after the write.
- [ ] Address components are written to their own OpenEMR columns
      (`street`/`line1`, `city`, `state`, `postal_code`, and `street2` when
      present), not concatenated into `street`. Confirmed by reading the
      stored row, not just by asserting on the request body.
- [ ] `PatientDemographicsUpdateService.php` accepts the new address fields
      and still rejects a request with no recognised field at all, rather
      than silently succeeding with nothing written.
- [ ] The existing all-four onboarding completion path
      (`OnboardingFlow.complete`, `ai_server/onboarding/flow.py:240-247`)
      keeps working unchanged, and now also stores the structured
      components rather than a flattened `street`.
- [ ] The confirmed-only rule from TICK-016 still holds: an unconfirmed or
      partially-validated address is never written.
- [ ] Every write remains audited per FR-17.

## Testing

Integration tests against a real OpenEMR (not a stub) covering: an
address-only update leaves name and DOB intact; each address component
lands in its own column; an invalid state or ZIP is refused before any
write; the onboarding completion path still writes all four plus
structured address. Extend `ai_server/tests/test_openemr_demographics.py`
and `test_onboarding_flow.py`. CI must be green.

## Out of Scope

Any chat-facing conversation, prompting, or confirmation UI — that is
TICK-050. Demographic fields beyond name, date of birth, and address
(TICK-016's Out of Scope still applies). Adding a `country` field, which
exists at no layer today. Revisiting TICK-043's decision that mononyms are
unsupported.
