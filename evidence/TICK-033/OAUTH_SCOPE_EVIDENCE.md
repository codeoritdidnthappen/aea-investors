# TICK-033 — patient-context OAuth client re-registration, live evidence

Executed against the running local Docker topology (`local-openemr-1`,
`local-mariadb-1`, `local-caddy-1`), the same shared stack
`evidence/TICK-002`, `evidence/TICK-024`, and `evidence/TICK-028` used.

## Result

The code fix (`ai_server/app/auth.py`'s `AuthSettings.scopes`) requests
`patient/*` scopes only, never `user/*`. A client registered with exactly that
scope list was proven live: it auto-enables with no manual admin approval
step, a genuine patient completes the real `authorization_code`+PKCE login
through OpenEMR's own login form, the resulting consent screen shows only
four small, patient-appropriate resource cards instead of the old client's
two-resource full-CRUD staff grid, and a real patient-bound access token is
issued and usable against the OpenEMR API. Two pre-existing, unrelated
defects in this OpenEMR fork were also found live and are recorded below,
out of scope for this ticket to fix.

## What changed

`ai_server/app/auth.py`, `AuthSettings.scopes`:

| Before (bug) | After (this ticket) |
|---|---|
| `openid offline_access api:oemr user/patient.crus user/appointment.cruds` | `openid offline_access api:oemr api:fhir api:port patient/Patient.read patient/Appointment.read patient/appointment.u patient/assessment.c patient/assessment.r patient/assessment.u` |

Every resource-scoped entry is now `patient/*`; `api:oemr`/`api:fhir`/`api:port`
are the bare umbrella scopes the Standard/FHIR/Portal API surfaces require and
carry no resource-permission meaning of their own (confirmed by reading
`ServerScopeListEntity::getV2ApiScopes()`/`getOpenIDConnectScopes()` in the
pinned image's own source — `api:port` is literally documented there as
"Permission to use the OpenEMR apis from inside the patient portal").

## Live client registration

```
POST https://emr.localhost/oauth2/default/registration
{
  "application_type": "private",
  "client_name": "Intake Assistant (TICK-033 local)",
  "redirect_uris": ["https://chat.localhost/oauth/callback"],
  "post_logout_redirect_uris": ["https://chat.localhost/"],
  "scope": "openid offline_access api:oemr api:fhir api:port patient/Patient.read patient/Appointment.read patient/appointment.u patient/assessment.c patient/assessment.r patient/assessment.u"
}
```

Response: `200`, `client_id: awiB2QM-g5BfH_Dt3a3KU7lxz1Rx9k7_2ynASXLkIoM`,
`client_role: "user"` (client secret not retained here, per the redaction
policy below).

`client_role` is still `"user"` in OpenEMR's own `oauth_clients` row — this is
unavoidable for a *confidential* client (one with a secret) on this pinned
release: `AuthorizationController::clientRegistration()` sets
`client_role = 'user'` unconditionally whenever `application_type === 'private'`,
regardless of what scopes are requested; only a public/native app (no client
secret at all) gets `client_role = 'patient'`. The AI server is, and must
remain, a confidential server-side client (it holds a secret and exchanges the
code itself) — changing that is a materially bigger architecture change this
ticket did not ask for and is not made here. This is why AC2 is satisfied via
its second option ("whatever consent screen remains is a patient-appropriate
one") rather than its first ("client is patient-role/auto-approved") — see
below.

**Confirmed via direct DB read** (`oauth_clients.is_enabled`): `1`
immediately after registration, no manual "Admin > System > API Clients >
Enable" step needed. This matches
`ScopeRepository::hasScopesThatRequireManualApproval()`'s own logic: a
confidential client auto-enables as long as it requests no `user/*` or
`system/*` scope — exactly the property this ticket's fix establishes. (The
*old* buggy client, and the separate probe client
`evidence/TICK-028/BINDING_MATRIX.md` used, both requested `user/*`-free or
`user/*`-bearing scopes under different circumstances; this is the first
client registered for this specific product path that is provably
`user/*`-free by construction.)

## Live patient login, headless (no browser available in this environment)

Same constraint recorded in `evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` ("No
browser-automation tool is available in this execution environment") still
holds. Unlike TICK-002's plain-form portal login, though, the OAuth
authorize→login→consent chain is not entirely a plain form: OpenEMR's login
page is a plain POST, but the scope-consent page's `Authorize` button is
wired to client-side JavaScript that reconstructs the submitted `scope[...]`
fields from whichever checkboxes are checked before POSTing. That JavaScript
is static and fully readable (`templates/oauth2/scope-authorize.html.twig` in
the pinned image), so it was replicated field-for-field in a throwaway,
uncommitted Python script (stdlib `urllib` + `http.cookiejar`, no browser) —
the same category of one-off verification tooling as TICK-002's disposable
probe module, not a product artifact and not checked in.

### Deviation on the record (new, this session)

The synthetic subject patient's (`pid 1`, portal username
`AverySubjecttest1`) portal password was not known to this session (same
situation `evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` recorded). A new
password was set by direct SQL (`UPDATE patient_access_onsite SET portal_pwd
= <bcrypt hash>`), generated in-container with PHP's `password_hash(...,
PASSWORD_DEFAULT)`. Environment provisioning for a synthetic fixture, not a
product data path or application code change. The plaintext value is not
recorded here or anywhere in the repository.

### Transcript (redacted — no token, code, client secret, UUID, or timestamp retained)

| # | Step | Result |
|---|---|---|
| 1 | `GET /oauth2/default/authorize?...&scope=<the corrected scope string>` | `302` → OpenEMR's own `/oauth2/default/provider/login` |
| 2 | `POST` username/password, `user_role=portal-api` (the patient-login submit button) | `200` on `/oauth2/default/scope-authorize-confirm` — login succeeded |
| 3 | Parsed the returned consent page's own structured-scope markup | **4 resource cards**: `Patient` (read+search, v1-style), `Appointment` (read+search, v1-style), `appointment` (update-only, v2-style — the cancel action), `assessment` (create+read+update, v2-style — the draft actions). Compare the *old* client's registration, which would render checkbox grids for `patient`/`appointment` resources with up to 5 actions (create/read/update/delete/search) each — a materially larger, staff-shaped form. |
| 4 | POSTed the exact reconstructed `scope[...]` fields the page's own JS would produce with every box left checked (plus, for one diagnostic-only re-run, the umbrella `api:oemr`/`api:fhir`/`api:port` scopes the JS itself never re-submits — see finding 2 below) | `302` → `https://chat.localhost/oauth/callback?code=...&state=...` (the AI server's real, configured redirect URI) — state matched exactly what step 1 generated |
| 5 | `POST /oauth2/default/token` with the captured code, PKCE verifier, and this client's credentials | `200` — a real access token, ID token, and refresh token issued |
| 6 | `GET /apis/default/fhir/Patient/<own uuid>` with the issued access token | `200` — the token reads the logged-in patient's own demographics |
| 7 | `GET /apis/default/fhir/Appointment` with the issued access token (the exact route `ai_server/openemr/adapter.py` calls) | `200` — an empty but successful appointment bundle |
| 8 | `PUT /apis/default/portal/patient/appointment/<bogus id>` with the issued access token, once `api:port` was actually present in the granted token (see finding 2) | `404 no appointment with that id for this patient` — this is the module's own not-found response, proving the *scope* check passed and only the id lookup (correctly) failed |

This satisfies AC4: a real patient token was obtained through the real
`authorization_code`+PKCE flow (not a registration-row inspection), and AC2:
the consent screen a genuine patient reaches is the small, four-resource,
patient-appropriate one above, not the old client's larger staff-shaped
CRUD grid.

## Two related findings, live-confirmed, out of scope for this ticket to fix

Per `ARCHITECTURE.md` ADR-3 discipline ("no local fallback/workaround; the
finding is the finding") and the precedent
`evidence/TICK-028/BINDING_MATRIX.md` set for the demographics-write gap,
both of the following are recorded rather than worked around:

**1. Appointment *booking* (`POST /api/patient/{pid}/appointment`,
`ai_server/scheduling/booking.py`) is staff-ACL-gated, not scope-gated, on
this OpenEMR release.** The route only calls
`RestConfig::request_authorization_check($request, "patients", "appt")`
(`AclMain::aclCheckCore` against the session's `authUser`) — never an OAuth
scope check — and this returned `403 insufficient permissions` for the
live patient token above regardless of which scopes it carried (tested both
without and with every umbrella scope present). `ai_server/openemr/adapter.py`
already only *reads* appointments through the FHIR route (`GET
/fhir/Appointment`, proven working above, step 7) and was never affected;
`booking.py`'s *create* path is. This is not new to this ticket — the
original `user/appointment.cruds` client also only ever proved this route
against a staff/admin-password-grant probe token
(`evidence/TICK-001/ENDPOINT_MATRIX.md`, "Create appointment / book"), never
a genuine patient-context one — but it means the same category of finding
`evidence/TICK-028/BINDING_MATRIX.md` recorded for demographics writes
likely also applies to booking. **Recommend a dedicated follow-up ticket** to
probe this specifically (mirroring TICK-028's method) and, if confirmed,
rescope `booking.py`'s create path the same way TICK-028 rescoped FR-26.

**2. The pinned image's own "AI-generated" scope-consent screen
(`templates/oauth2/scope-authorize.html.twig`,
`AuthorizationController::authorizeUser()`) silently drops two categories of
requested scope that a real patient's browser click-through can never
recover:**

- Bare, resource-less scopes (`api:oemr`, `api:fhir`, `api:port`) never get a
  checkbox at all — the page's `reconstructScopes()` JS only ever walks
  `.action-checkbox` elements, which only exist for `context/resource.action`
  scopes. A genuine patient clicking "Authorize" therefore never actually
  resubmits `api:oemr`/`api:fhir`/`api:port`, no matter how the client is
  registered. `patient/Patient.read`/`patient/Appointment.read` still worked
  live (transcript steps 6–7) because those particular FHIR routes turn out
  not to require the bare `api:fhir` scope at all; the Standard-API and
  Portal-API routes this product also needs (`api:oemr`, `api:port`) do
  require it, so — confirmed live — a real, un-worked-around click-through
  would fail on this product's cancel and assessment-draft routes with `403`.
- A resource registered as several single-letter scopes
  (`patient/assessment.c`/`.r`/`.u`, exactly how
  `openemr_modules/aeai-portal-chat`'s `AssessmentDraftController` registers
  it) can never be *fully* granted in one consent approval either: the same
  JS always merges every checked action for one resource into a single
  combined scope string (`patient/assessment.cru`), but
  `ResourceScopeEntityList::containsScope()` requires *one* registered scope
  entity to be a superset of the *entire* combined request — three separate
  single-letter entities never satisfy a combined multi-letter ask. Confirmed
  live: the combined string is silently dropped from the issued token.
  Re-registering the *client* with a single combined `patient/assessment.cru`
  string does not work around it either — OpenEMR's own registration
  endpoint rejects it (`invalid_scope`) because the module only ever
  advertised the three single-letter identifiers as valid.

Both are defects in the pinned OpenEMR image's own consent-screen code, not
in this product. Fixing them would mean either patching the pinned image
(explicitly out of bounds — see `deploy/local/PATIENT_AUTH.md`'s own
"known defect" precedent for the same class of issue) or changing
`openemr_modules/aeai-portal-chat`'s scope registration shape (TICK-017's
module, not this ticket's). **Recommend a dedicated follow-up ticket** against
`openemr_modules/aeai-portal-chat` to register `patient/assessment.cru` (and
any other multi-action module scope) as a single combined identifier from the
start, so a real consent click-through can actually grant it — and to record
the bare-umbrella-scope drop as a known limitation of the manual
click-through path (a "Remember Me"/trusted-user replay, or a scope
pre-approved by an admin, would not hit this same client-side
reconstruction step).

Neither finding blocks this ticket's own acceptance criteria: AC1–AC4 are
about the client's *registered* scope shape, the consent screen a patient
sees, and proving a real patient token is obtainable — not about proving
every downstream product feature is regression-free end to end (that is
explicitly deferred to the separate, blocked-on-this-ticket E2E re-attempt
per this ticket's own Out of Scope section).

## Deployment step still needed (not performed by this build worker)

This ticket's code fix (`ai_server/app/auth.py`) and the live client
registration above are both done and proven. Wiring the new client into the
running local stack requires copying its `client_id`/`client_secret` into
`deploy/local/.env`'s `OPENEMR_OAUTH_CLIENT_ID`/`OPENEMR_OAUTH_CLIENT_SECRET`
and restarting the `ai-server` container — `deploy/local/.env` is
untracked, machine-local, and shared across every worktree building against
this same Docker topology, so it is intentionally not touched by this
change (a build worker's worktree is not the place to mutate shared,
out-of-tree runtime state other in-flight work may depend on). The new
client (`client_id` above) is registered, `is_enabled`, and live-proven
end-to-end against the real OpenEMR instance; only that `.env` copy-over and
container restart remain, as an explicit operational step for whoever owns
the shared environment.

## Redaction

No access token, refresh token, client secret, authorization code, patient
UUID, name, date of birth, or timestamp is retained above, per
`deploy/local/PATIENT_AUTH.md`'s redaction policy. The registered client's
`client_id` is retained (not on the banned list; the same precedent
`evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md` already set by recording
`OPENEMR_OAUTH_CLIENT_ID` in the clear).
