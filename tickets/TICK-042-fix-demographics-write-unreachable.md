---
id: TICK-042
title: "bug(onboarding): demographics write route is structurally unreachable for a genuine patient token"
type: task
epic: EPIC-07
priority: P0
estimate: M
depends_on: [TICK-016, TICK-035, TICK-040, TICK-041]
labels: [onboarding, openemr, auth]
source: [FR-6, FR-17, FR-26, NFR-25]
status: done
remote_url:
---
## Context

Found live 2026-08-20 while independently re-verifying TICK-024's desktop E2E
coverage now that TICK-038/039/040/041 are all resolved: a real patient
(`AverySubjecttest1`) completing the full guided-onboarding conversation
through the actual chat UI reached the final review step, replied `CONFIRM`,
and got "We couldn't finish saving your onboarding just now; your progress
is saved. Please try confirming again." -- every time, consistently
reproducible. Onboarding could never actually complete for a genuine
patient.

Root-caused directly from the pinned image's own source, the identical
pattern TICK-040 already found and fixed for booking:
`OpenEmrDemographicsAdapter.write_confirmed_demographics()`
(`ai_server/openemr/demographics.py`) called the Standard API route
`PUT /api/patient/:puuid` (`apis/routes/_rest_routes_standard.inc.php:92`).
That route's handler calls
`RestConfig::request_authorization_check($request, "patients", "demo")`
(`PatientRestController::put()`), which resolves to
`AclMain::aclCheckCore($section, $value, $request->getSession()->get("authUser"), ...)`
-- a **staff ACL** check against a logged-in OpenEMR staff username, not an
OAuth scope check at all. A genuine patient-context OAuth session has no
staff ACL identity at all -- `aclCheckCore()` cannot ever succeed for one.
This is structurally impossible for the current route to ever accept a real
patient token, regardless of what scope is requested or granted -- exactly
the same class of gap TICK-040 root-caused for
`POST /api/patient/:pid/appointment`.

`OnboardingFlow.complete()` requires both the demographics write and the
draft-completion write to succeed before reporting completion (by design,
`ONBOARDING_CONTRACT.md` "Draft and completion semantics" #5: on failure,
retain the draft and show a retry message) -- so this one unreachable call
silently blocked 100% of onboarding completions for genuine patients, with
no error surfaced anywhere except a generic retry message and no log entry
(the exception was caught and swallowed by
`OnboardingChatService._handle_confirmation`'s `except OpenEmrRequestError`
branch, by design, matching the documented retry contract -- but with
nothing underneath ever able to succeed on retry).

## Acceptance Criteria

- [x] A new module-added portal route (`RestApiCreateEvent`, the same
      mechanism `AppointmentBookController` (TICK-040) already uses,
      registered under `openemr_modules/aeai-portal-chat`) exposes a
      patient-writable demographics update
      (`PatientDemographicsController`/`PatientDemographicsUpdateService`,
      `PUT /portal/patient/demographics`), enforced by
      `AuthorizationListener`'s OAuth-scope check, not staff ACL.
- [x] The new route's scope (`patient/demographics.u`) is added to the AI
      server's requested/registered client scopes (`AuthSettings.scopes`,
      `ai_server/app/auth.py`) and consented on the OAuth screen -- proven
      live through a real consent flow (the existing local client's
      registered scope updated, a fresh login, every checkbox submitted
      checked including the new "demographics" resource), not assumed to
      just work because a scope string was added.
- [x] `OpenEmrDemographicsAdapter` is repointed from `PUT /api/patient/:puuid`
      to the new portal route and no longer sends or needs a patient id at
      all (the module route resolves it server-side from the bearer token,
      matching `OpenEmrBookingAdapter`'s TICK-040 contract exactly); the
      Standard API route is not used for a patient-context demographics
      write anywhere in this codebase after this ticket.
- [x] A genuine patient completing the full guided-onboarding conversation
      through the real chat UI reaches "Thanks, `<name>`! Your onboarding is
      complete and saved to your OpenEMR record." -- proven live end to end
      (not a probe against a mocked response): real OAuth login, real chat
      turns through every field, real `CONFIRM`, and a real, correctly
      populated `patient_data` row (`fname`, `lname`, `DOB`, `street`) after
      completion.

Full evidence: `evidence/TICK-042/DEMOGRAPHICS_WRITE_ROUTE_EVIDENCE.md`.

## Testing

Live verification against the local Docker topology: updated the registered
client's scope and re-consented through a real OAuth flow, then completed
the entire onboarding conversation through the real chat UI (`chat.localhost`)
as a genuine patient and confirmed the resulting `patient_data` row. CI is
green: `pytest ai_server/tests/` -- 433 passed, 3 skipped, 90.69% coverage;
`ruff format --check`/`ruff check` clean.

## Out of Scope

A UI/flow mechanism to edit a single already-answered identity field from the
review step without restarting identity capture (pre-existing gap, unrelated
to this ticket's route-reachability fix; identity fields are documented as
in-memory-only, restart-durable draft fields are not affected). Populating
`patient_data.city`/`state`/`postal_code` as separate columns instead of one
combined `street` line (pre-existing `_format_address()` behavior, unchanged
by this ticket). A confirmed mononym (empty family name) failing OpenEMR's
own `PatientValidator` (`lname` must not be empty) -- live-confirmed
pre-existing, present on the old unreachable route too had it ever been
reachable, not a regression from this fix; tracked separately as TICK-043.

## Review findings (fixed)

First code-review pass found one real issue, fixed:
`OnboardingChatService._handle_confirmation` still gated completion on
`session_store.patient_uuid(handle, now)` being present, a leftover from the
pre-TICK-042 contract -- the demographics write no longer needs it (the
module route resolves it server-side from the token, matching
`BookingTool`'s own TICK-040 fix). `patient_uuid` is documented best-effort
(`ai_server/app/auth.py:190-195`); its absence didn't mean the token itself
couldn't write. A genuine patient with a fully valid, write-capable token was
being incorrectly told to "sign out and sign back in" for no real reason.
Test rewritten (`test_completion_without_a_bound_patient_uuid_still_completes_tick_042`)
to lock in the corrected behavior.

Also flagged (verified, tracked separately, not blocking): the mononym
validation gap above (TICK-043).
