# TICK-028 — patient-context binding matrix

Token obtained by authorization_code + PKCE with a portal patient login.
No password grant, no staff credential, no `users` row was used.

Redaction: no token, refresh token, client secret, authorization code, UUID,
name, date of birth, or timestamp is retained below.

| Attempt | Route | Status | Result |
|---|---|---|---|
| read own | `GET /apis/default/fhir/Patient/<REDACTED_UUID>` | 200 | ALLOWED |
| read other | `GET /apis/default/fhir/Patient/<REDACTED_UUID>` | 500 | denied |
| write own | `PUT /apis/default/api/patient/<REDACTED_UUID>` | 403 | denied |
| write other | `PUT /apis/default/api/patient/<REDACTED_UUID>` | 403 | denied |

## Verdict: READ-ONLY

Binding holds, but the patient token cannot write its own chart. The product cannot write demographics as the patient on v8.3.0; TICK-016's staff-credential path must be removed and the requirement rescoped.

Record this outcome in `evidence/TICK-001/ENDPOINT_MATRIX.md`, replacing the
pending demographics-write row.
