# TICK-042: Demographics write route evidence

## Finding

Live-verified 2026-08-20 through the real chat UI (`https://chat.localhost/`)
as patient `AverySubjecttest1`: completing the full guided-onboarding
conversation and replying `CONFIRM` at the review step consistently returned

> We couldn't finish saving your onboarding just now; your progress is
> saved. Please try confirming again.

Reproduced twice in a row (identical result) before investigating further.

## Root cause

`OpenEmrDemographicsAdapter.write_confirmed_demographics()`
(`ai_server/openemr/demographics.py`, pre-fix) called the Standard API route:

```
PUT /api/patient/:puuid
```

Read directly from the pinned image's own source
(`apis/routes/_rest_routes_standard.inc.php:92-98`):

```php
"PUT /api/patient/:puuid" => function ($puuid, HttpRestRequest $request) {
    RestConfig::request_authorization_check($request, "patients", "demo");
    $data = (array) (json_decode(file_get_contents("php://input")));
    $return = (new PatientRestController())->put($puuid, $data, $request);
    return $return;
},
```

`RestConfig::request_authorization_check()` resolves to
`AclMain::aclCheckCore($section, $value, $request->getSession()->get("authUser"), ...)`
-- a **staff ACL** check against a logged-in OpenEMR staff username, never an
OAuth scope check. A genuine patient-context OAuth session has no staff ACL
identity at all, so this route can never accept a real patient token --
identical to the gap TICK-040 already found and fixed for
`POST /api/patient/:pid/appointment`.

`OnboardingFlow.complete()` requires this call to succeed before reporting
completion, by design (retain-and-retry on failure). Since the call could
never succeed, no genuine patient could ever complete onboarding, and no
error surfaced beyond the generic retry message shown above -- the
`OpenEmrRequestError` was caught and swallowed by
`OnboardingChatService._handle_confirmation`'s own documented retry-message
path, and nothing was logged.

## Fix

Mirrors TICK-040's booking fix exactly:

- New module route `PUT /portal/patient/demographics`
  (`PatientDemographicsController`/`PatientDemographicsUpdateService`,
  `openemr_modules/aeai-portal-chat`), enforced by `AuthorizationListener`'s
  OAuth-scope check instead of staff ACL. Delegates to
  `PatientService::update()` -- real, callable OpenEMR business logic, the
  same class of call `AppointmentBookService` uses for booking.
- `patient/demographics.u` scope added to `AuthSettings.scopes`
  (`ai_server/app/auth.py`).
- `OpenEmrDemographicsAdapter` repointed to the new route, reusing the same
  `OpenEmrPortalSettings` booking/cancellation/draft already use, and no
  longer sends a patient id at all -- OpenEMR resolves it server-side from
  the bearer token via `HttpRestRequest::getPatientUUIDString()`.
- `OnboardingFlow.complete()` and `write_confirmed_demographics()` dropped
  the now-unused `patient_uuid` parameter.

## Live proof

### 1. Registered client's scope updated and re-consented

The existing local OAuth client's registered `scope` column
(`oauth_clients` table) was updated to add `patient/demographics.u`:

```
openid offline_access api:oemr api:fhir api:port patient/Patient.read
patient/Appointment.read patient/appointment.c patient/appointment.u
patient/assessment.c patient/assessment.r patient/assessment.u
patient/demographics.u
```

A fresh patient login (`AverySubjecttest1`) was required to mint a new
access token carrying the new scope -- the prior session's token, minted
before the scope was added, failed with:

```
OpenEMR.ERROR: scope patient/demographics.u not in access token
{"...","path":"/apis/dispatch.php/default/portal/patient/demographics"}
```

confirming both that the route now enforces the new scope correctly, and
that a token without it is correctly refused (not silently accepted). The
new consent screen showed a "demographics" resource-permission row
(`PATIENT` badge) alongside the existing appointment/assessment/patient
rows, submitted with every checkbox checked.

### 2. Full onboarding conversation completed through the real chat UI

Every turn sent through the actual `chat.localhost` message box (not a
scripted API call):

1. "I'd like to start my onboarding." -> asked for contact method
2. `{"method": "email", "value": "avery.subjecttest@example.com"}`
3. `counseling_or_therapy`
4. `{"format": "video", "time_window": "weekday_afternoon"}`
5. `{"selected": []}`
6. `Avery`
7. `Subjecttest`
8. `1995-04-12`
9. `{"street1": "100 Maple Ave", "city": "Springfield", "state": "IL", "zip_code": "62704"}`
10. Review step shown correctly with every field.
11. `CONFIRM` ->

> Thanks, Avery! Your onboarding is complete and saved to your OpenEMR
> record.

No retry message, no error -- first attempt succeeded after the scope fix
and fresh token.

### 3. Real, correctly populated `patient_data` row

```
$ docker exec local-mariadb-1 mariadb -u openemr -p*** openemr -e \
    "SELECT pid, fname, lname, DOB, street FROM patient_data WHERE fname='Avery' AND lname='Subjecttest'\G"

pid: 1
fname: Avery
lname: Subjecttest
DOB: 1995-04-12
street: 100 Maple Ave, Springfield, IL 62704
```

Patient uuid (converted from the stored `binary(16)` value via
`UuidRegistry::uuidToString()`): `a28b0cf9-f4c8-4674-81fc-ec99365c12bb`.

`city`/`state`/`postal_code` remain empty -- pre-existing, unrelated
behavior: `_format_address()` has always combined the address into one
`street` line rather than populating those columns separately (unchanged by
this ticket, noted as Out of Scope).

## An earlier attempt in this same session hit a documented, unrelated gap

Before the scope/token issue above, an earlier completion attempt (using
the *original* access token, before the client's scope was updated) failed
identically to the original bug report -- confirming the fix wasn't yet
live. After deploying the code fix but before re-authenticating, a
container restart (required to load the new `ai-server` image) cleared the
in-memory per-session `_SessionState.identity` dict, so the identity fields
had to be re-collected; this is documented, pre-existing behavior
(`OnboardingFlow`'s identity fields are explicitly not restart-durable,
unlike draft fields, which persist through OpenEMR itself) and not a
regression from this fix.

## Test suite

`pytest ai_server/tests/`: 433 passed, 3 skipped, 90.69% coverage.
`ruff format --check ai_server/` / `ruff check ai_server/`: clean.
