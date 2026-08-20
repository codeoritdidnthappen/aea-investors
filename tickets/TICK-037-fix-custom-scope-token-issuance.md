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
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/74
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

## Root Cause (confirmed 2026-08-20, live)

Neither remaining hypothesis above was right. The actual drop happens on the
consent screen itself, client-side, before the approval POST is even sent.

`templates/oauth2/scope-authorize.html.twig` is not a custom file in this
project -- it is OpenEMR 8.3.0's own real, upstream template (merged via
openemr/openemr#9457 and #9466, "granular scopes", closing issue #8639;
confirmed against the real `v8_3_0` tag on GitHub). Its `reconstructScopes()`
JS has two independent bugs that only manifest for a `RestApiScopeEvent`
module scope like `assessment`, never for this app's core FHIR scopes:

1. `reconstructV2Scope()` only emits a scope input via its restricted-category
   loop or its `unrestricted`-flagged else-branch. A resource with **no**
   restriction sub-categories and `isUnrestricted=false` (the module's
   server-side scope-structuring code apparently defaults unrecognized custom
   resources to `false`) falls into neither branch -- its checkbox displays
   checked, but nothing is ever added to the submitted form. Confirmed via a
   client-side interceptor on `form.submit()`: before the fix, `assessment`
   never appeared in the POST body at all.
2. Once made to emit something, the original code joined every checked
   action into one combined string per resource (e.g. `patient/assessment.cru`
   for create+read+update). OpenEMR's `ResourceScopeEntityList::containsScope()`
   (`src/Common/Auth/OpenIDConnect/Entities/ResourceScopeEntityList.php`)
   checks each of a resource's *individually registered* scope entities on
   its own and never unions their permissions across entries. Since the
   module registers `patient/assessment.c`, `.r`, `.u` as three separate
   single-action `addScope()` calls (`AssessmentDraftController::addScopes()`),
   no single registered entity alone covers a combined `.cru` request, so it
   silently failed `containsScope()` in `AuthorizationController::
   updateAuthRequestWithUserApprovedScopes()` and never made it into the
   persisted `AuthorizationRequest`, `finalizeScopes()`, or the issued token.
   Confirmed by reading `ScopeEntity::containsScope()`,
   `ScopeValidatorFactory::buildScopeValidatorArray()`, and
   `ResourceScopeEntityList` directly, then reproducing exactly this failure
   live with the first (action-joining) version of the fix.

Fix: `reconstructV2Scope()` now emits one atomic `${context}/${resource}.${action}`
scope string per checked action (never joined), and resources with no
restriction options honor the master checkbox directly instead of falling
into the restricted-only branch. This is strictly compatible with core FHIR
resources too (an atomic single-action scope is always contained by a
resource whose scopes happen to be registered pre-combined), so nothing that
worked before regresses.

Since this is a genuine bug in the pinned release's own vendor file rather
than anything this project authored, the fix is a targeted override, not a
modification of the checked-out image: the patched file lives at
`openemr_overrides/templates/oauth2/scope-authorize.html.twig` and is
bind-mounted read-only over the vendor path in `deploy/local/docker-compose.yml`,
the same pattern already used for `openemr_modules/aeai-portal-chat` (TICK-012).
No core file inside the container/image itself was edited in place.

Live proof: a real `/oauth/launch` login as Avery, through consent, to a real
onboarding-start chat turn, produced `POST /apis/dispatch.php/default/portal/patient/assessment`
returning **201** (previously: 401, `"scope patient/assessment.c not in
access token"`), and inserted a real `draft` row into `aeai_assessment_draft`
for the correct `patient_uuid`. The scope-drop bug is conclusively fixed.

A **separate, new bug** surfaced during this same verification pass: the AI
server's own response handling (`ai_server/onboarding/draft_client.py`) fails
to parse that 201 response (`OpenEMR returned an invalid assessment draft
response`) even though the OpenEMR-side row is correct -- filed as TICK-038,
out of scope for this ticket.

Cancellation's `patient/appointment.u` (TICK-036) was never affected by the
join bug specifically -- it only ever has one checked action, so nothing was
ever joined for it. It may or may not have been independently affected by
the drop bug, depending on its resource's server-side `isUnrestricted` flag
(not yet isolated which value it carries); either way, this same fix covers
it going forward. Live verification of a real cancellation attempt is
TICK-036's own responsibility to prove, not re-listed as an open item here.

## Acceptance Criteria

- [x] Root cause is confirmed with direct evidence (not just the two
      remaining hypotheses above) for why a custom `RestApiScopeEvent`
      scope is absent from an issued access token despite being consented
      and present on the client's registration.
- [x] A genuine patient login through the real `/oauth/launch` flow, followed
      by a real onboarding-start turn, succeeds in creating an assessment
      draft through `POST /portal/patient/assessment` -- proven live, not
      just against a raw HTTP probe. (OpenEMR-side creation confirmed live;
      the AI server's own response parsing hits a separate bug, TICK-038.)
- [x] No core OpenEMR file is modified to fix this (ADR/ARCHITECTURE.md's
      standing constraint); the fix lives in the module's own registration
      code, an AI-server-side workaround, or documents a genuine, unfixable
      platform limitation if that's what's found. (Bind-mounted override,
      not an in-place edit to the checked-out vendor file.)

Full evidence: `evidence/TICK-037/SCOPE_DROP_EVIDENCE.md`.

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
