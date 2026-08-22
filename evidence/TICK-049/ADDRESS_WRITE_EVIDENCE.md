# TICK-049: address-only write and structured address columns

Live proof against the running local stack (`deploy/local`): OpenEMR **8.3.0**
(`local-openemr-1`) and MariaDB **11.8.8** (`local-mariadb-1`), verified
2026-08-22. Reproduce with `scripts/probe_demographics_address.php` — it
creates its own synthetic patient through OpenEMR's `PatientService`, drives
the real `PatientDemographicsUpdateService`, reads `patient_data` back column
by column, and deletes the row it created.

## What this proves, and what it deliberately does not

The probe calls the service directly rather than over HTTPS. The layer above
it — `PatientDemographicsController`, the `patient/demographics.u` OAuth scope,
`AuthorizationListener`, and the token-derived `getPatientUUIDString()` — is
**unchanged by this ticket** and was already proved end to end through the real
chat UI in `evidence/TICK-042/DEMOGRAPHICS_WRITE_ROUTE_EVIDENCE.md`. What
changed is which fields the service accepts and which columns they land in, so
that is what is exercised here, against real OpenEMR business logic and a real
database rather than a stub. The probe still asserts the 401 path, so the
"no token-bound patient" refusal is covered too.

## Schema read first, not guessed

`BaseService::buildUpdateColumns` silently drops any key that is not a real
`patient_data` column, so a wrong wire name would look like success and write
nothing. The column names were read off the live schema before being chosen:

```
$ docker exec local-mariadb-1 mariadb -u root -p*** openemr -e "SHOW COLUMNS FROM patient_data"

street          varchar(255)
street_line_2   tinytext
city            varchar(255)
state           varchar(255)
postal_code     varchar(255)
```

There is no `line1` column — `street` is street line 1. `state` is backed by
OpenEMR's `state` option list, whose `option_id`s are the two-letter codes
`validate_address` already enforces, so a stored `IL` renders as `Illinois` in
the staff UI rather than as a stray string:

```
$ ... "SELECT option_id, title FROM list_options WHERE list_id='state' AND option_id IN ('IL','CA','PR')"
CA  California
IL  Illinois
PR  Puerto Rico
```

## Why a partial write is safe

Read out of the pinned image's own source:

- `PatientService::update()` (`src/Services/PatientService.php:307`) builds its
  `UPDATE` from `buildUpdateColumns($data)` — **only the keys actually passed**
  appear in the `SET` clause, so an omitted field is left alone, not blanked.
- `PatientValidator::configureValidator()`
  (`src/Validators/PatientValidator.php`) copies the insert context into
  `DATABASE_UPDATE_CONTEXT` with `$chain->required(false)` on every rule and
  adds only `uuid` as required. So `fname`/`lname`/`DOB` are **not** required
  on an update, which is what makes an address-only write possible at all.

## Probe run

Every check passed (27/27). Abridged output; full script in `scripts/`.

```
probe patient uuid: a28fa308-348b-4e38-8fb8-86a118168e95

--- 0. as created: flattened street, empty city/state/postal_code
    fname = 'Tick049'  lname = 'Probesubject'  DOB = '1988-03-09'
    street = '1 Old Street, Oldtown, IL 60601'
    street_line_2 = NULL  city = ''  state = ''  postal_code = ''
```

### AC1 + AC2 — address-only write, structured columns

```
1. address-only PUT: 200 {"status":"updated"}
   body: {"street":"42 Oak St","street_line_2":"Apt 4B","city":"Springfield",
          "state":"IL","postal_code":"62704"}     <- no fname/lname/DOB at all

--- 1. after the address-only write
    fname = 'Tick049'   lname = 'Probesubject'   DOB = '1988-03-09'   <- untouched
    street = '42 Oak St'
    street_line_2 = 'Apt 4B'
    city = 'Springfield'
    state = 'IL'
    postal_code = '62704'

PASS - 1. given name untouched
PASS - 1. family name untouched
PASS - 1. date of birth untouched
PASS - 1. street holds line 1 only
PASS - 1. street_line_2 / city / state / postal_code each in their own column
PASS - 1. nothing was concatenated into street
```

Compare `evidence/TICK-042/…:124-141`, where the same write produced
`street: 100 Maple Ave, Springfield, IL 62704` with `city`/`state`/`postal_code`
empty. That is the gap this ticket closes.

### AC3 — a body with nothing recognised in it is refused

```
2.  empty body:              400 {"error":"validation failed","details":[
      "at least one demographics field is required: fname, lname, DOB, street,
       street_line_2, city, state, postal_code"]}
2b. {"nickname":"Ticky"}:    400 {"error":"validation failed","details":[
      "nickname is not a writable demographics field"]}

PASS - 2. refused requests changed nothing
```

Unknown fields are now refused rather than silently dropped: before this
ticket, `validatedFields` iterated the whitelist and ignored everything else,
so a request asking to write a field this endpoint cannot write could still
come back `200 updated`.

### AC4 — the onboarding completion path still works, now structured

```
3. full onboarding PUT: 200 {"status":"updated"}
   body: {"fname":"Avery","lname":"Van Der Berg","DOB":"1990-01-01",
          "street":"100 Maple Avenue","street_line_2":"","city":"Springfield",
          "state":"IL","postal_code":"62704"}

--- 3. after the onboarding completion write
    fname = 'Avery'  lname = 'Van Der Berg'  DOB = '1990-01-01'
    street = '100 Maple Avenue'  street_line_2 = ''
    city = 'Springfield'  state = 'IL'  postal_code = '62704'
```

The multi-word family name still survives intact (TICK-043).

### A defect this probe found, and the fix

The first run of this probe omitted `street_line_2` from the request whenever
the patient gave no second line — the obvious reading of "write `street2` when
present". Reading the row back showed why that is wrong:

```
--- after moving to an address with no apartment line (street_line_2 omitted)
    street = '7 Birch Ln'   city = 'Peoria'   postal_code = '61602'
    street_line_2 = 'Apt 4B'        <- stale, from the PREVIOUS address
```

Because an omitted field is left alone, the old apartment line survived onto
the new address and the stored record was a blend of two addresses — exactly
what this ticket exists to prevent, since TICK-050 shows the patient a parsed
address that must match what is stored. An address is therefore written as one
whole unit: `_address_body` always sends `street_line_2`, empty when there is
no second line, and `PatientDemographicsUpdateService` allows an empty value
for that one field (`CLEARABLE_STRING_FIELDS`) so it can be cleared. After the
fix:

```
4. second address-only PUT: 200
--- 4. after the second address-only write
    fname = 'Avery'  lname = 'Van Der Berg'  DOB = '1990-01-01'   <- still intact
    street = '7 Birch Ln'  street_line_2 = ''  city = 'Peoria'
    state = 'IL'  postal_code = '61602'
```

An empty value is still refused for every other field, so nothing else can be
blanked by an empty string:

```
5. empty 'fname'       : 400 {"…":["fname must be a non-empty string"]}
5. empty 'lname'       : 400 …
5. empty 'DOB'         : 400 …
5. empty 'street'      : 400 …
5. empty 'city'        : 400 …
5. empty 'state'       : 400 …
5. empty 'postal_code' : 400 …
```

### AC5 / AC6 — confirmed-only, and still on OpenEMR's own write path

The confirmed-only rule is enforced AI-server side, before any request exists:
`ConfirmedAddress` can only be built by `confirm_address`, which re-runs
`fields.validate_address` at the write boundary, so an invalid state code, a
malformed ZIP, a blank city, or a `None` address raises
`IdentityNotConfirmedError` and no HTTP call is made. Covered by
`test_openemr_demographics.py::test_tick_049_ac5_…` and
`test_onboarding_flow.py::test_tick_049_completion_refuses_an_invalid_state_or_zip_before_any_write`,
which asserts `demographics_writes == []`.

FR-17 holds unchanged: the service contains no SQL and still writes through
`(new PatientService())->update(...)`, OpenEMR's own business logic, which
fires `BeforePatientUpdatedEvent`/`PatientUpdatedEvent` as usual. Asserted in
CI by `test_demographics_module.py::test_the_write_still_goes_through_openemrs_own_patient_service_not_sql`.

```
6. no bound patient: 401 {"error":"no bound patient on this request"}
PASS - cleanup: the probe patient row is gone
ALL CHECKS PASSED
```

## Test suite

`uv run --locked --group dev pytest`: 472 passed, 4 skipped, 90.86% coverage.
`ruff format --check .` / `ruff check .`: clean.
`php -l` on the changed module file: no syntax errors.
