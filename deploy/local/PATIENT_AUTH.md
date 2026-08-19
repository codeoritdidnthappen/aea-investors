# Local patient-context authentication

The product must act as the **logged-in patient**, never as staff. This document sets the
local stack up so that boundary is real, then proves it with a committed probe.

Nothing here uses the password grant, an admin credential, or a `users` row. If the flow
below cannot be completed, that is the finding — record it and stop, do not fall back.

Prerequisites: `deploy/local/docker compose up -d` healthy, `emr.localhost` reachable
over HTTPS through Caddy.

---

## 1. Enable the API and portal surfaces

These are OpenEMR configuration globals. They ship **off** and there is no API to set
them — this is environment provisioning, not a product data path, so the admin UI is the
correct tool.

Log into `https://emr.localhost` as the seeded admin **once**, for configuration only.
No product code path may ever use this account.

**Administration → Config → Connectors:**

| Setting | Value | Why |
|---|---|---|
| Enable OpenEMR Standard REST API | on | `/apis/default/api` |
| Enable OpenEMR Standard FHIR REST API | on | `/apis/default/fhir` |
| Site Address Override (`site_addr_oath`) | `https://emr.localhost` | OAuth2 issuer and redirect handling break silently without it |

**Administration → Config → Portal:**

| Setting | Value |
|---|---|
| Enable Patient Portal | on |
| Allow Portal Appointments | on |

Leave portal **self-registration** off. We provision the patient deliberately (step 2)
rather than going through the 11-step self-registration flow, which additionally
requires Google reCAPTCHA keys and SMTP.

Set a single allowed language rather than "All Languages Allowed" — the multi-language
default is a documented cause of portal failures.

---

## 2. Create two synthetic patients with portal logins

Two, not one. The second exists solely so the probe can attempt cross-patient access.

For each patient, in the OpenEMR UI:

1. **Patient → New/Search** → create the patient. Only first name, last name, DOB, and
   sex are actually required.
2. Open the patient → **Demographics → Choose → Portal** tab.
3. Set a portal **username** and **password**. Record them; they are the only
   credentials the probe uses.

Record which is the *subject* (the one who will log in) and which is the *other* (the
one the probe will try to reach and must not).

> If OpenEMR forces a credential reset on first portal login, complete that once in the
> browser so the probe's login is a plain username/password step.

Verify before continuing: log into `https://emr.localhost/portal` as the subject
patient. If that fails, nothing downstream will work.

---

## 3. Register a patient-scoped OAuth client

Register via `curl`, not the admin UI — the UI has a known defect that silently
uppercases scopes and produces 401s that look like OAuth failures.

```bash
curl -sk -X POST https://emr.localhost/oauth2/default/registration \
  -H 'Content-Type: application/json' \
  -d '{
    "application_type": "private",
    "client_name": "Intake Assistant (local probe)",
    "redirect_uris": ["http://localhost:8910/callback"],
    "post_logout_redirect_uris": ["http://localhost:8910/"],
    "scope": "openid fhirUser offline_access api:oemr api:fhir patient/Patient.read patient/Patient.write patient/Appointment.read"
  }' | tee /tmp/oauth-client.json
```

Scopes are pinned to the registration row — you cannot request one later that you did
not register. Keep them lowercase and exact.

**Then enable the client.** A confidential client requesting `patient/*` scopes is
created with `is_enabled = 0`. Go to **Administration → System → API Clients**, find it,
click **Enable**. Skipping this produces `invalid_client` with no useful message.

Put the returned `client_id` / `client_secret` into `deploy/local/.env`.

---

## 4. Run the probe

```bash
uv run --locked python -m scripts.probe_patient_context \
  --base-url https://emr.localhost \
  --client-id "$OPENEMR_OAUTH_CLIENT_ID" \
  --client-secret "$OPENEMR_OAUTH_CLIENT_SECRET" \
  --other-patient-uuid "<uuid of the second patient>" \
  --output evidence/TICK-028
```

It opens a browser at OpenEMR's authorize endpoint. **Log in as the subject patient**
and consent. The probe captures the callback, exchanges the code with PKCE, and runs
the binding matrix.

Never enter an admin credential at that prompt. Doing so invalidates the entire run.

---

## 5. What the probe decides

It writes `evidence/TICK-028/BINDING_MATRIX.md` with four outcomes:

| | Own chart | Other chart |
|---|---|---|
| **Read** | expect success | expect denial |
| **Write** | unknown | **this is the question** |

- **Other-chart write denied** → binding is enforced. Patient-context writes are
  legitimate. Re-probe TICK-016 under this token and delete the staff path.
- **Other-chart write succeeds** → a patient token can modify another patient's chart.
  That is an upstream security finding, not a capability. The route is permanently
  rejected and the product performs no demographic write on v8.3.0.
- **No token obtainable** → the product's authorization premise does not hold on this
  release and the scope must change.

Record the outcome in `evidence/TICK-001/ENDPOINT_MATRIX.md`, replacing the pending row.

---

## Redaction

The probe redacts before writing evidence: no access token, refresh token, client
secret, authorization code, UUID, name, date of birth, or timestamp is retained. Only
HTTP status, whether the response body was an error, and the pass/fail verdict.

Verify with `git diff` before committing. If a secret is in the evidence file, the run
is discarded, the client is deleted, and the stack is rebuilt.
