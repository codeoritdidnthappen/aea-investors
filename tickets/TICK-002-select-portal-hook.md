---
id: TICK-002
title: "spike(portal): select a supported patient-portal iframe hook"
type: spike
epic: EPIC-01
priority: P1
estimate: M
depends_on: [TICK-001]
labels: [openemr, portal, discovery]
source: [FR-1, FR-2, FR-3, FR-4]
status: in_progress
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/3
---

## Context

The OpenEMR integration must use a supported hook on the pinned release so the patient
remains in the portal and browser JavaScript never receives OpenEMR tokens.

The previous `blocked_reason` — "no browser integration is available" — no longer holds.
A pinned local stack now runs at `https://emr.localhost` with a trusted Caddy internal
CA, and a synthetic patient can authenticate against the real portal.

## Selected hook — `OpenEMR\Events\PatientPortal\RenderEvent`

**Source evidence (read from the pinned tag, not from docs):**

- [`src/Events/PatientPortal/RenderEvent.php` @ v8_3_0](https://github.com/openemr/openemr/blob/v8_3_0/src/Events/PatientPortal/RenderEvent.php)
- [`portal/home.php` @ v8_3_0](https://github.com/openemr/openemr/blob/v8_3_0/portal/home.php) —
  imports `RenderEvent` (line 26) and passes all four constants into the Twig render
  context (lines 405–408).

Four injection points are published:

| Constant | Value | Injects |
|---|---|---|
| `EVENT_DASHBOARD_INJECT_CARD` | `home.dashboard.inject.card` | A Bootstrap card on the portal Dashboard |
| `EVENT_SECTION_RENDER_POST` | `home.section.render.post` | HTML after all portal SPA sections render |
| `EVENT_SCRIPTS_RENDER_PRE` | `home.scripts.render.pre` | Scripts, before any other script loads |
| `EVENT_DASHBOARD_RENDER_SCRIPTS` | `home.dashboard.render.scripts` | Supporting JS for the injected card |

`RenderEvent`'s own docblock ships an example Bootstrap card containing an anchor to an
external URL — launching an external app from the portal is the documented intent of this
hook, not a repurposing of it.

**Why this satisfies the logged-out criterion by construction.** These events are
dispatched only from `portal/home.php`, which renders only for an authenticated portal
session. The hook cannot appear to a logged-out visitor because the page that fires it
never renders for one. This is a structural property of the extension point, not a check
the module adds and could forget.

**Consumption path:** a Symfony event subscriber inside an OpenEMR custom module under
`interface/modules/custom_modules/`, registered through Modules → Manage Modules.

⚠️ **Not yet verified:** the exact custom-module scaffolding and registration contract for
v8.3.0. Confirmed so far is that the events exist and are dispatched. Do not treat the
module layout as settled until it is read from the pinned source.

## Rejected alternatives

- **Direct SMART-on-FHIR app launch from the portal.** The
  `Enable OpenEMR SMART ON FHIR Context Test Launches` global exists and is off by
  default, and its own label warns it is for those who know what they are doing. Not
  established as the supported patient-facing launch path on this release.
- **Patching portal templates in place.** Not an extension point; would not survive an
  upgrade and is not a "supported hook" under this ticket's own terms.

## Acceptance Criteria

- [x] A supported extension hook is identified for the pinned release, with source
      evidence from the pinned tag.
- [ ] The installation path (custom-module scaffolding + registration) is documented
      against v8.3.0 source.
- [ ] A minimal synthetic-patient proof shows the hook is unavailable when logged out and
      launches inside the portal when logged in.
- [ ] The proof identifies the direct AI-server callback and confirms the iframe issues no
      OpenEMR API calls.

## Environment established (2026-08-20)

Recorded because the proof depends on it and the setup was not reproducible from the
repository before now.

- Pinned stack up at `https://emr.localhost` (Caddy internal CA trusted on the host).
- Standard REST API, FHIR REST API enabled; `site_addr_oath = https://emr.localhost`;
  OAuth2 password grant **off**.
- Patient portal enabled; `Patient Portal Site Address = https://emr.localhost/portal`.
- Two synthetic patients: **pid 1 Avery Subjecttest** (subject, portal login working),
  **pid 2 Jordan Controlcase** (cross-patient target for TICK-028).
- OAuth client registered; registration accepted `patient/Patient.write`.
  `OAuth2 App Manual Approval Settings` reads *"Patient standalone apps Auto Approved"*.

### Environment deviations — on the record

1. **`portal_pwd_status` set to 1 by direct SQL** for the synthetic patients. OpenEMR
   creates portal credentials with status `0`, whose activation path is the patient-facing
   reset flow; that flow could not be completed locally. This is environment provisioning
   for a synthetic fixture, **not** a product data path, and does not relax the
   `ENDPOINT_MATRIX.md` prohibition on database workarounds in application code.
2. **Portal must be entered at `/portal/index.php?site=default`.** Entering at `/portal`
   leaves the portal session's `site_id` empty; `get_patient_info.php`'s multisite
   mismatch handler then bounces the user to `interface/login/login.php` — which is
   indistinguishable from a rejected credential from the outside. Any automated proof must
   use the `?site=default` entry point.

### Failure-mode reference (from `PatientPortalLoginController::login`)

Login failures encode their cause in the redirect query string. Recorded here because it
cost an afternoon to discover:

| Redirect suffix | Cause |
|---|---|
| `&w&u` | username not found in `portal_login_username` |
| `&w&p` | password failed `AuthHash::passwordVerify` |
| `&w&c` | `uname`/`pass` absent from the POST |
| `&w` | `allow_patient_portal != 'YES'`, `enforce_signin_email` mismatch, or lost session |

Note also that `portal_pwd_status = 0` does **not** reject a login: the controller
authenticates first, then redirects to the change-password form.

## Testing

Exercise login, launch, and logout in the pinned local OpenEMR Docker stack with local
browser network capture; attach redacted evidence to `evidence/TICK-002/`. CI must be
green.

## Out of Scope

Implementing the final portal module (TICK-012).
