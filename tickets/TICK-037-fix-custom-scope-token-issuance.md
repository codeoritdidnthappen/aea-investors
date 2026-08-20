---
id: TICK-037
title: "bug(auth): custom module-registered scopes never reach the issued access token"
type: task
epic: EPIC-03
priority: P1
estimate: M
depends_on: [TICK-017, TICK-035]
labels: [auth, openemr, oauth]
source: [FR-5, FR-8, FR-27, NFR-25]
status: todo
---

## Context

Found live 2026-08-20 running TICK-035's onboarding flow end-to-end for the
first time through a real browser (not TICK-033/035/036's own scripted
proofs): every attempt fails with

```
ai_server.openemr.adapter.OpenEmrRequestError: OpenEMR assessment draft request failed with status 401
```

OpenEMR's own log names the exact reason:

```
message: "scope patient/assessment.c not in access token"
```

Confirmed this is not a registration, consent, or client-config problem:

- `oauth_clients.scope` for the AI server's client includes
  `patient/assessment.c/r/u` (verified directly, and via a fresh client
  registered through the real `POST /oauth2/default/registration` endpoint,
  matching TICK-033's own proven approach -- same result both times).
- The consent screen shows "assessment" as a grantable resource, and
  `oauth_trusted_user.scope` records `patient/assessment.c/r/u` as approved
  after a real patient clicks Authorize.
- The 401 is a *scope-recognized-but-absent* error
  (`AuthorizationListener::onRestApiSecurityCheck`,
  `"scope " . $scope . " not in access token"`), not an *unknown scope*
  error -- so the scope name itself is valid at request-time; it just isn't
  in the token that was actually issued for this authorization.

`patient/assessment.c/r/u` is a **custom** scope
(`openemr_modules/aeai-portal-chat/src/Controller/AssessmentDraftController
::addScopes()`, registered via `RestApiScopeEvent::
EVENT_TYPE_GET_SUPPORTED_SCOPES`), unlike `patient/Patient.read`/
`patient/Appointment.read`, which are core OpenEMR FHIR scopes present in
`ServerScopeListEntity::getAllSupportedScopesList()` before any module event
ever fires. Confirmed live: booking (TICK-034), which only uses core
scopes, works end-to-end with a real patient login -- the same session,
same token, same request. Onboarding, which needs the custom `assessment`
scope, does not.

Reading `ScopeRepository` (`src/Common/Auth/OpenIDConnect/Repositories/
ScopeRepository.php`): `finalizeScopes()` builds its allow-list from
`$clientEntity->getScopes()` directly (the client's own DB row -- confirmed
correct), so the drop isn't there. `getCurrentSmartScopes()`/
`getScopeEntityByIdentifier()` (used during `validateAuthorizationRequest()`
at the `/authorize` step) dispatches `RestApiScopeEvent` for
`API_TYPE_FHIR`/`API_TYPE_STANDARD` only -- not `API_TYPE_PORT` -- though the
module's own listener doesn't discriminate by type, so that alone doesn't
explain it either. The most likely remaining explanation, not yet confirmed:
the OAuth `/authorize`/`/token` PHP entry points bootstrap a different/leaner
module set than `apis/dispatch.php` (the real REST/FHIR/Portal request
path), so the module's `RestApiScopeEvent` listener is registered by the
time an actual `POST /portal/patient/assessment` call checks whether the
scope name is *known*, but not by the time the OAuth grant resolves what
scopes go *into* the token. Needs direct verification (e.g., temporary
logging in `ScopeRepository`/`AuthorizationController` around a real
authorize+token exchange) before attempting a fix.

`AppointmentCancelController::addScopes()` (TICK-036) registers
`patient/appointment.u` the identical way and almost certainly hits the same
bug -- not yet verified live, since onboarding failed first and cancellation
was not reached this pass.

## Acceptance Criteria

- [ ] Root cause is confirmed with direct evidence (not just the two
      remaining hypotheses above) for why a custom `RestApiScopeEvent`
      scope is absent from an issued access token despite being consented
      and present on the client's registration.
- [ ] A genuine patient login through the real `/oauth/launch` flow, followed
      by a real onboarding-start turn, succeeds in creating an assessment
      draft through `POST /portal/patient/assessment` -- proven live, not
      just against a raw HTTP probe.
- [ ] The same live proof for cancellation's `patient/appointment.u` scope
      (TICK-036), confirming whether it was independently affected.
- [ ] No core OpenEMR file is modified to fix this (ADR/ARCHITECTURE.md's
      standing constraint); the fix lives in the module's own registration
      code, an AI-server-side workaround, or documents a genuine, unfixable
      platform limitation if that's what's found.

## Testing

Live verification against the local Docker topology: a real
`authorization_code`+PKCE patient login through `/oauth/launch`, a real
onboarding-start chat turn, and a real cancellation attempt, all the way
through to a confirmed OpenEMR-side write (not just a 200 status). CI must
be green.

## Out of Scope

Redesigning the module's scope-registration mechanism from scratch if a
targeted fix is found. Booking (TICK-034, confirmed unaffected -- core
scopes only).
