# TICK-040 — module-added portal booking route, live evidence

Executed against the running local Docker topology (`local-openemr-1`,
`local-mariadb-1`, `local-caddy-1`, `local-ai-server-1`), the same shared stack
`evidence/TICK-002`, `evidence/TICK-024`, `evidence/TICK-028`, `evidence/TICK-033`,
`evidence/TICK-037`, `evidence/TICK-039`, and `evidence/TICK-041` used.

## What changed

- `openemr_modules/aeai-portal-chat/src/Service/AppointmentBookService.php` (new) and
  `.../Controller/AppointmentBookController.php` (new): a module-added
  `POST /portal/patient/appointment` route, registered via the same
  `RestApiCreateEvent`/`RestApiScopeEvent` mechanism `AppointmentCancelController`
  (TICK-036/041) already uses -- enforced by `AuthorizationListener`'s OAuth-scope
  check, not the Standard API's staff-ACL check TICK-040's own investigation found
  structurally blocks a genuine patient token. The caller's numeric OpenEMR patient
  id is resolved server-side from the bearer token
  (`PatientService::getPidByUuid()`, converted through `UuidRegistry::uuidToBytes()`
  first -- the identical binary(16)-column fix TICK-041 already proved necessary for
  `AppointmentCancelService`), never accepted as client input.
- `ai_server/scheduling/booking.py`: `OpenEmrBookingAdapter` now calls the new portal
  route instead of the Standard API route, and no longer takes or sends a patient id
  at all (the route resolves it itself). `OpenEmrBookingSettings` removed; reuses
  `OpenEmrPortalSettings` (the same settings class cancellation already uses).
  `BookingService.book()` no longer takes `patient_id` either; its two call sites
  (`ai_server/app/chat.py`, `ai_server/scheduling/reschedule.py`) updated to match.
- `ai_server/app/auth.py`: `AuthSettings.scopes` gains `patient/appointment.c`
  alongside the existing `patient/appointment.u`.

## Live proof

A disposable script (stdlib `re`/`html`/`hashlib`/`base64` plus `httpx`, no browser
-- the same technique `evidence/TICK-033` and `evidence/TICK-041`'s own disposable
scripts used) drove a **real** `authorization_code`+PKCE login as the seeded patient
`AverySubjecttest1`, against a **freshly registered** OAuth client
(`POST /oauth2/default/registration`, matching TICK-033's own proven approach) whose
scope list includes the new `patient/appointment.c`. Every consent checkbox was
submitted checked, replicating exactly what a patient clicking "Authorize" with
nothing unchecked would submit.

One real, non-obvious finding along the way: the consent page's actual form
`action` is `/oauth2/default/device/code` (`AuthorizationController::
DEVICE_CODE_ENDPOINT`, confirmed in `OAuth2AuthorizationListener::authorizeRequest()`),
not the page's own URL (`scope-authorize-confirm`) -- posting to the wrong URL
silently re-renders the same consent page with a `200`, easy to mistake for a
validation failure. Once corrected, the real token exchange succeeded:

```
POST /oauth2/default/token  -> 200, real access_token
POST /apis/default/portal/patient/appointment
  Authorization: Bearer <real token>
  {"pc_catid": 9, "pc_title": "AI-scheduled visit", "pc_duration": 900,
   "pc_eventDate": "2026-08-25", "pc_startTime": "14:00",
   "pc_facility": 3, "pc_billing_location": 3}
  -> 201 {"id": "a28d30a8-4b94-48d1-b6c4-9739c5c82f80", "status": "booked"}
```

Confirmed directly in the database -- a real row, correctly bound to the token's
own patient, resolved entirely server-side:

```
pc_eid=8, uuid=a28d30a8-4b94-48d1-b6c4-9739c5c82f80, pc_pid=1 (Avery Subjecttest,
  the same synthetic patient the token authenticated as -- never supplied by the
  caller), pc_eventDate=2026-08-25, pc_startTime=14:00:00, pc_apptstatus='-',
  pc_title='AI-scheduled visit'
```

This is the exact route `BookingService`'s call now hits (confirmed by reading its
own updated `_APPOINTMENT_PATH`), proven with the real consent flow, a real issued
token, and a real OpenEMR-side write -- not a probe against a hand-crafted token or
a mocked response.

## What this does and doesn't unblock

The Standard API route this replaces is provably, structurally unreachable for a
genuine patient token (staff ACL check) -- this route is not. `BookingService`/
`RescheduleService`'s own composition (TICK-020) both inherit this fix
automatically; no separate change was needed in either once `OpenEmrBookingAdapter`
was repointed (confirmed: their own test suites, unmodified beyond the constructor
argument type, still pass).

This does **not** make booking reachable through a natural chat conversation today:
`open_slots` is still always empty (`NoMappedCandidateSource`, ADR-3's separate,
already-known "no candidate-slot source" gap -- explicitly out of scope for this
ticket), so the model never has a real `slot_token` to select. `_SCHEDULING_RULES.
booking_enabled` was left `False` (unchanged) -- it is purely advisory prompt
context with no code-level gate today, and flipping it would not unlock any new
functionality while `open_slots` stays empty regardless.

## Environment provisioning (2026-08-20)

- Registered a fresh OAuth client via the real `/oauth2/default/registration`
  endpoint (matching TICK-033's own proven approach) with the updated scope list;
  `deploy/local/.env`'s `OPENEMR_OAUTH_CLIENT_ID`/`_SECRET` updated to match. The
  previous client remains registered and unused.
- The synthetic patient's portal password was reset via direct SQL
  (`password_hash()` via `AuthHash`, matching the established precedent in
  `evidence/TICK-033`/`TICK-041`) to complete this live login; the plaintext value
  is not retained anywhere in this repository.
- `local-ai-server-1` was rebuilt (code is baked into the image, not bind-mounted)
  and `local-openemr-1` was restarted (clears PHP opcache for the bind-mounted
  module change) to deploy both halves of this fix before live verification.
