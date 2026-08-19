# TICK-001 — authenticated synthetic-patient probe record

**Pinned target:** OpenEMR `v8_3_0` / `openemr/openemr:8.3.0`
**Run:** 2026-08-19
**Environment:** disposable Docker Compose stack bound to `127.0.0.1` only. It used
only a fresh database, a locally registered OAuth client, the seeded local admin, and
one synthetic patient. The stack and its volumes were removed after the probe.

## Redaction policy

No client ID, client secret, bearer token, UUID, person name, date of birth, phone
number, facility ID, provider ID, event ID, or timestamp is retained below. Values in
the actual request/response transcript were replaced with `[REDACTED]` before this
evidence was written.

## Results

| Probe | Redacted request | Redacted result | Outcome |
|---|---|---|---|
| OAuth client registration | `POST /oauth2/default/registration` with `openid api:oemr user/patient.crus user/appointment.cruds` | `200`; confidential local client returned client credentials | Pass |
| Token issue | `POST /oauth2/default/token` using the enabled local client, password grant, and synthetic-local admin | `200`; `{ "access_token":"[REDACTED]", "scope":"openid user/patient.crus user/appointment.cruds" }` | Pass for isolated local testing only. Password grant is not a production flow. |
| Create synthetic patient | `POST /apis/default/api/patient` with `user/patient.crus` | `201`; `{ "data": { "pid":"[REDACTED]", "uuid":"[REDACTED]" } }` | Pass |
| Read synthetic demographics | `GET /apis/default/api/patient/[REDACTED_UUID]` | `200`; `{ "data": { "fname":"[REDACTED]", "lname":"[REDACTED]", "DOB":"[REDACTED]" } }` | Pass |
| Update confirmed synthetic demographic | `PUT /apis/default/api/patient/[REDACTED_UUID]` with one confirmed synthetic phone field | `200`; response contained only the updated synthetic record | Pass |
| Book appointment | `POST /apis/default/api/patient/[REDACTED_PID]/appointment` with all required appointment fields | `200`; `{ "id":"[REDACTED_EVENT_ID]" }` | Pass |
| Read appointment | `GET /apis/default/api/patient/[REDACTED_PID]/appointment` and `GET /apis/default/api/appointment/[REDACTED_EVENT_ID]` | Both `200`; returned the synthetic appointment | Pass |
| Delete appointment route | `DELETE /apis/default/api/patient/[REDACTED_PID]/appointment/[REDACTED_EVENT_ID]` | `404`; `{ "message":"record not found" }` immediately after a successful create/read | Fails locally; it is not usable for cancellation and would not meet FR-14 in any event. |

## Deliberate non-probes and remaining gates

The local user-context token does not establish SMART EHR-launch or patient-context
authorization. No test used a real patient, a production credential, direct database
data access, delete/recreate rescheduling, or form-table write. Source-confirmed gaps
for availability, office hours, closures, reschedule, status-preserving cancellation,
and assessment writes remain implementation-blocking as listed in `ENDPOINT_MATRIX.md`.

## Test / CI result

The repository gate passed after the probe:

```text
uv run --locked --group dev ruff format --check .
uv run --locked --group dev ruff check .
uv run --locked --group dev pytest
1 passed
```
