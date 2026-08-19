# TICK-001 — authenticated synthetic-patient probe record

**Pinned target:** OpenEMR `v8_3_0` / image references in `ENDPOINT_MATRIX.md`
**Recorded:** 2026-08-18
**Status:** `NOT RUN — BLOCKED`

## Why no runtime request/response transcript is asserted

This worktree contains no OpenEMR deployment, synthetic-patient seed, registered OAuth
client, or authorized test credentials. No authenticated request was sent, and no
request/response pair below is fabricated. The release-source evidence in the matrix
is therefore the preserved redacted evidence available for this ticket attempt.

Runtime probes cannot resolve the four source-confirmed missing API surfaces
(availability/office-hours/closures, reschedule, status cancellation, and assessment
writes). They remain implementation blockers even after a future environment exists.

## Redacted probe plan for endpoints that do exist

Run this plan only in an isolated v8.3.0 deployment populated solely with a disposable
synthetic patient. Store the command headers/body and response under this directory
after replacing bearer tokens, authorization codes, launch values, UUIDs, names, dates
of birth, addresses, provider IDs, facility IDs, and timestamps with `[REDACTED]`.

| Probe | Request evidence to preserve | Expected redacted response evidence | Gate |
|---|---|---|---|
| EHR launch | `GET /oauth2/default/authorize` with `response_type=code`, `aud`, `launch=[REDACTED]`, PKCE, state and patient scopes | redirect to registered callback with `code=[REDACTED]` and matching `state=[REDACTED]` | Token response must contain only the launch-bound synthetic patient context. |
| Token exchange | `POST /oauth2/default/token` with authorization code and PKCE verifier | `{ "access_token":"[REDACTED]", "scope":"[REDACTED]", "patient":"[REDACTED_UUID]" }` | Verify state/nonce and never retain raw output in logs. |
| Appointment read | `GET /apis/default/fhir/Appointment` with bearer token | FHIR Bundle containing only the launch-bound synthetic patient's Appointment resources | Confirm cancelled records can be filtered by returned status. |
| Demographic read | `GET /apis/default/fhir/Patient/[REDACTED_UUID]` with bearer token | FHIR Patient resource, with all demographic values redacted in evidence | Confirm another patient UUID is denied. |
| Demographic write candidate | `PUT /apis/default/fhir/Patient/[REDACTED_UUID]` using only confirmed synthetic values | Either confirmed updated Patient or a 4xx authorization response | Required to establish patient-scoped write enforcement; until then FR-26 remains blocked. Roll back only by the same API if the write succeeds. |

Do **not** probe booking with a staff/user token, delete cancellation, or any direct
database operation: each would violate the documented NFR-25/FR-14/FR-17 boundary and
cannot make the required feature acceptable.

## Test / CI result

No project test command or test runner exists in this worktree. No CI configuration was
changed. Runtime acceptance cannot be green because the required authenticated synthetic
environment is absent and source-confirmed endpoint gaps block the requested operations.
