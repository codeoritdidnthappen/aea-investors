# TICK-041 — `AppointmentCancelService::forPatient()` 404 root cause, live evidence

Executed against the running local Docker topology (`local-openemr-1`,
`local-mariadb-1`), the same shared stack `evidence/TICK-002`, `evidence/TICK-024`,
`evidence/TICK-028`, and `evidence/TICK-033` used.

## The finding

`AppointmentCancelService::forPatient()` (`openemr_modules/aeai-portal-chat/src/
Service/AppointmentCancelService.php`) called `AppointmentService::search(['puuid'
=> $patientUuid, 'pc_uuid' => $auuid])` with `$patientUuid`/`$auuid` as plain
36-character dashed UUID strings (`UuidRegistry::uuidToString()`'s format) — the
identical two-filter call OpenEMR's own built-in
`AppointmentRestController::getOneForPatient()` also makes. Read directly from the
pinned image's own source:

- `openemr_postcalendar_events.uuid` and `patient_data.uuid` are both `binary(16)`
  columns (confirmed live: `SHOW COLUMNS ... LIKE 'uuid'` on both tables).
- A bare string/number value passed into `AppointmentService::search()` is turned
  into a `StringSearchField` with `SearchModifier::PREFIX` unless the caller wraps
  it (`FhirSearchWhereClauseBuilder::build()`), and — critically —
  `SearchFieldStatementResolver::resolveStringSearchField()`'s `EXACT` branch
  resolves to `"BINARY " . $field . ' = ?'`, binding the raw string as-is. There is
  no code path in `search()` that converts a dashed UUID string to the packed bytes
  the column actually stores.

A `binary(16)` column can never equal a 36-byte string, so this call always
returned zero rows — regardless of whether `$patientUuid`/`$auuid` were genuinely
correct — for every caller of this exact pattern on this pinned release, not just
this module.

## Live proof (real DB, real `AppointmentService::search()`, not just the generated SQL)

Ticket's own repro ids, confirmed directly in the database:

```
openemr_postcalendar_events: pc_eid=7, HEX(uuid)=A28CFEE8F65A488AB186253E2D609A7C,
  pc_apptstatus='-', pc_pid=1
patient_data: pid=1, HEX(uuid)=A28B0CF9F4C8467481FCEC99365C12BB
```

(`a28cfee8-f65a-488a-b186-253e2d609a7c` is exactly the appointment uuid the
ticket's OpenEMR access log shows in the failing `PUT` request.)

**1) Raw SQL, both forms, against the real table:**

```sql
SELECT COUNT(*) FROM openemr_postcalendar_events
 WHERE BINARY uuid = 'a28cfee8-f65a-488a-b186-253e2d609a7c';        --> 0
SELECT COUNT(*) FROM openemr_postcalendar_events
 WHERE BINARY uuid = UNHEX('A28CFEE8F65A488AB186253E2D609A7C');    --> 1
```

**2) The real, unmodified `AppointmentService::search()` method itself** (PHP CLI,
bootstrapped the same way `bin/console` bootstraps a DB-connected command:
`$ignoreAuth = true; $sessionAllowWrite = true; require 'interface/globals.php';`,
run as the `apache` user per `RootCliGuard`'s own requirement — a one-shot script,
not a server, terminating on its own):

```php
// BEFORE FIX: dashed-string uuids (the module's pre-fix call)
(new AppointmentService())->search(['puuid' => $patientUuidString, 'pc_uuid' => $auuidString]);
// -> row count: 0

// AFTER FIX: UuidRegistry::uuidToBytes() converted first
(new AppointmentService())->search([
    'puuid' => UuidRegistry::uuidToBytes($patientUuidString),
    'pc_uuid' => UuidRegistry::uuidToBytes($auuidString),
]);
// -> row count: 1, pc_eid: 7, pc_apptstatus: -
```

This is the same conversion `InsuranceRestController::put()`/`::patch()`/`::delete()`
already perform before every `puuid` search this pinned release ships with
(`UuidRegistry::isValidStringUUID($puuid)` then `UuidRegistry::uuidToBytes($puuid)`)
— an established, in-image convention this module's `search()` call had simply not
followed.

## The fix

`AppointmentCancelService::forPatient()` now validates both ids with
`UuidRegistry::isValidStringUUID()` (an invalid/malformed id is treated the same as
"not found" — `cancel()`'s existing 404 — never an uncaught exception) and converts
both with `UuidRegistry::uuidToBytes()` before calling `search()`. No core OpenEMR
file is touched; the fix is entirely inside this module, matching this project's
existing "bind-mount over vendor path, never patch the pinned image" discipline for
product-owned code (core-template patches like TICK-037's remain the one narrow,
explicitly-recorded exception, and are not what this is).

## What this does and doesn't prove

Proven live: the *query* that was silently returning zero rows now returns exactly
the right row, executed through the real `AppointmentService` class against the
real database — not a reimplementation or a mocked search. Also proven live (see
`evidence/TICK-041/FABRICATED_SUCCESS_FIX_EVIDENCE.md`): a real `PUT` from this
worktree's own `AppointmentCancelAdapter` against the still-pre-fix running
`local-openemr-1` container genuinely reproduces the ticket's exact 404 for this
same appointment.

Not performed by this build worker: deploying this PHP fix into the shared,
already-running `local-openemr-1` container (it bind-mounts
`openemr_modules/aeai-portal-chat` from the main checkout, not this worktree —
`evidence/TICK-033/OAUTH_SCOPE_EVIDENCE.md`'s own "Deployment step still needed"
note records the same boundary) and re-running the ticket's exact end-to-end repro
against the deployed fix to confirm `pc_eid=7` now genuinely cancels. That is an
explicit operational step for whoever owns the shared environment, the same
precedent TICK-033 recorded rather than performing itself from inside a build
worker's worktree.

## Redaction

No access token, refresh token, client secret, authorization code, patient UUID
beyond the two already-public repro ids the ticket itself cites, or timestamp is
retained above, per `deploy/local/PATIENT_AUTH.md`'s redaction policy. The synthetic
patient's portal password was reset via direct SQL (bcrypt hash generated
in-container with PHP's `password_hash()`) to complete a headless login for the
companion Python-side evidence file — the same precedent
`evidence/TICK-033/OAUTH_SCOPE_EVIDENCE.md`'s "Deviation on the record" section set;
the plaintext value is not recorded here or anywhere in the repository.
