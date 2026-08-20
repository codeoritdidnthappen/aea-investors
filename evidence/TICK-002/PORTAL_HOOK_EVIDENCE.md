# TICK-002 portal-hook evidence

## Result

All four acceptance criteria are satisfied. The previously blocking `PROBE_EVIDENCE`
disposable module ran end to end against the pinned local OpenEMR stack
(`openemr/openemr:8.3.0`, containers already up per the ticket's "Environment
established" record) using an HTTP-session probe (cookie-jar `curl`, not a
JavaScript-executing browser — see "Method note" below) instead of interactive
browser control, which remains unavailable in this environment.

## Pinned release and supported hook

`openemr/openemr:8.3.0`. Selected hook, re-confirmed by reading the pinned
container's own source tree (not GitHub) during this pass:

```php
OpenEMR\Events\PatientPortal\RenderEvent::EVENT_SECTION_RENDER_POST
// event name: home.section.render.post
```

## Installation path — verified against v8.3.0 source read from the running container

**Loader contract** (`src/Core/ModulesApplication.php`):

- `bootstrapCustomModules()` (line 141) selects modules with
  `SELECT mod_name, mod_directory FROM modules WHERE mod_active = 1 AND type != 1`.
  `MODULE_TYPE_CUSTOM = 0` (line 28) is the required `type` for a custom module —
  distinct from `MODULE_TYPE_LAMINAS = 1`, which the same table also stores and
  which this query deliberately excludes.
- `loadCustomModule()` (line 179) does
  `include $module['path'] . '/openemr.bootstrap.php'` inside a function whose
  parameters are `$classLoader`, `$module`, `$eventDispatcher` — all three are
  therefore in scope inside the included bootstrap file (PHP `include` shares the
  including scope). `CUSTOM_MODULE_BOOSTRAP_NAME = 'openemr.bootstrap.php'` (line 39,
  their typo, not mine) is the required filename.
- This selection query runs fresh on every request (no restart needed to pick up a
  newly enabled module, and none was needed in this probe — see below).

**Render contract** — how `RenderEvent::EVENT_SECTION_RENDER_POST` actually reaches
the page:

- `portal/home.php` (lines 404–409) passes the four `RenderEvent` constants into the
  Twig context as `eventNames.sectionRenderPost` etc.
- `templates/portal/home.html.twig` line 964:
  `{{ fireEvent(eventNames.sectionRenderPost) }}`.
- `src/Common/Twig/TwigExtension.php` lines 193–201 define the `fireEvent` Twig
  function: it output-buffers (`ob_start()`/`ob_get_clean()`) a
  `$kernel->getEventDispatcher()->dispatch(new GenericEvent($eventName, ...), $eventName)`
  call and inlines whatever the listener(s) echoed. This is why a listener registered
  with `$eventDispatcher->addListener(RenderEvent::EVENT_SECTION_RENDER_POST, $callable)`
  can simply `echo` HTML.

**Real precedent module** — the bundled `oe-module-comlink-telehealth` (ships in the
pinned image at `interface/modules/custom_modules/oe-module-comlink-telehealth/`)
subscribes to the same event the same way:
`src/Controller/TeleHealthPatientPortalController.php` line 33:
`$eventDispatcher->addListener(RenderEvent::EVENT_SECTION_RENDER_POST, $this->renderTeleHealthPatientVideo(...))`,
and its handler (line 36) just `echo`s a Twig-rendered fragment. Its
`openemr.bootstrap.php` (repo root of that module) is the minimal shape: register a
PSR-4 namespace via `$classLoader->registerNamespaceIfNotExists(...)`, instantiate a
`Bootstrap` class, call `subscribeToEvents()`.

**Database registration contract** (`modules` table, confirmed by schema + the
loader query above):

| Column | Required value |
|---|---|
| `mod_directory` | must equal the module's directory name under `interface/modules/custom_modules/` |
| `type` | `0` (custom module; `1` is reserved for Laminas/Zend modules and is excluded by the loader query) |
| `mod_active` | `1` |

No `mod_directory` row existed for any custom module on this instance before this
probe (only the five bundled `type=1` Laminas modules were registered), so there was
no example row to copy — the above was derived directly from the loader's SQL, not
from an existing row.

**Consumption path for TICK-012:** an `openemr.bootstrap.php` entrypoint under
`interface/modules/custom_modules/<module-directory>/`, subscribing to
`RenderEvent::EVENT_SECTION_RENDER_POST`, registered by inserting a `modules` row
with `type=0`, `mod_active=1`, and enabling via **Modules → Manage Modules** in the
admin UI (the DB write above stood in for the UI click in this probe only, for the
same reason `portal_pwd_status` was set by SQL — see deviation log).

## Minimal synthetic-patient proof

### Method note

No browser-automation tool is available in this execution environment (checked
again this session; still absent). The 2026-08-20 environment note says the earlier
`No browser is available` blocker "no longer holds" — that refers to the *portal
stack* being reachable and a synthetic patient being able to authenticate, not to a
new browser-automation tool becoming available. None did. This proof therefore uses
a cookie-jar `curl` session against the real HTTP endpoints instead: the portal login
(`portal/get_patient_info.php`) is a plain session-cookie form POST with no
JavaScript or OAuth redirect chain involved (confirmed by reading
`PatientPortalLoginController::login`), so a scripted HTTP client reproduces the
same request/response sequence a browser would issue for login, render, and logout.
It does **not** execute the returned HTML/JS, so it cannot itself load the iframe's
`src` — that target was instead independently fetched and its own response headers
inspected (see "AI-server callback" below). This is weaker than a real browser
capture and is called out here rather than silently presented as equivalent.

### Deviation on the record (new, this session)

The synthetic subject patient's (`pid 1`, `avery.subject@example.invalid`) portal
password was not known to this session (never recorded in the repository, and could
not be from a non-interactive worker). A new password was set by direct SQL
(`UPDATE patient_access_onsite SET portal_pwd = <bcrypt hash>`), generated in-container
with PHP's `password_hash(..., PASSWORD_DEFAULT)` to match `AuthHash`'s own algorithm.
This is the same category of deviation already on record for `portal_pwd_status`:
environment provisioning for a synthetic fixture, not a product data path, and not a
change to any application code path. The plaintext value is not recorded in this file
or anywhere in the repository. Portal login for `pid 1` was re-verified working after
the probe (see cleanup transcript below).

### Disposable probe module

A temporary module `aeai-portal-hook-proof` was placed at
`interface/modules/custom_modules/aeai-portal-hook-proof/openemr.bootstrap.php`
(not committed — removed at the end of this probe, per the installation-path
contract above):

```php
use OpenEMR\Events\PatientPortal\RenderEvent;

$eventDispatcher->addListener(RenderEvent::EVENT_SECTION_RENDER_POST, function ($event) {
    echo '<section id="aeai-portal-hook-proof" data-tick="TICK-002">'
        . '<iframe title="AI onboarding proof" data-aeai-probe="1" '
        . 'src="https://chat.localhost/oauth/launch"></iframe>'
        . '</section>';
});
```

Registered via `INSERT INTO modules (... mod_directory='aeai-portal-hook-proof',
type=0, mod_active=1 ...)`. No container restart was needed — the very next HTTP
request already loaded it, confirming the loader's "runs fresh every request" reading
above.

### HTTP transcript (redacted — no cookie values, tokens, or the probe password)

| # | Step | Request | Result |
|---|---|---|---|
| 1 | Logged out | `GET /portal/home.php` (no session) | `302` → `index.php?site=&w`; response body has no `aeai-portal-hook-proof` marker |
| 2 | CSRF setup | `GET /portal/index.php?site=default` | `200`; sets the `itsme` session flag `PatientPortalLoginController::login` requires |
| 3 | Login | `POST /portal/get_patient_info.php` with `uname`, `pass`, `site=default` | `302` → `portal/home.php` (a bare redirect with **no** `&w` suffix — per the ticket's own failure-mode table, only success produces this) |
| 4 | Authenticated render | `GET /portal/home.php` (session from step 3) | `200`; body contains exactly one `<section id="aeai-portal-hook-proof">...<iframe ... src="https://chat.localhost/oauth/launch"></iframe></section>` |
| 5 | Logout | `GET /portal/logout.php` | `302` → `index.php?site=default&logout` |
| 6 | Post-logout | `GET /portal/home.php` (same cookie jar) | `302` → `index.php?site=&w`; no `aeai-portal-hook-proof` marker — session was actually destroyed, not just the client-side cookie discarded |

Steps 1–6 were re-run once more after the disposable module and its `modules` row
were deleted (cleanup below): login still succeeds (`302` → `home.php`, `200` on
`home.php`), but the marker is now absent — confirming the marker's earlier presence
was caused by the module and not by caching or a probe artifact.

### AI-server callback and no-OpenEMR-API-call confirmation

The iframe's static `src` is `https://chat.localhost/oauth/launch` —
`ai_server/app/main.py`'s `GET /oauth/launch` route, which "start[s] a stateful PKCE
authorization-code launch" (its own docstring) and is reverse-proxied by
`deploy/local/Caddyfile` to the `ai-server` container only; `chat.localhost` never
routes to OpenEMR.

- The HTML OpenEMR sends to the browser (the module source above, and the captured
  step-4 response) contains no OpenEMR API/FHIR URL (`/apis/`, `/oauth2/`), no bearer
  token, no patient identifier, and no `fetch`/`XMLHttpRequest` call — the only
  browser-visible instruction is the static `<iframe src>` pointing at a different
  origin. This is the structural property the ticket's Context requires: "browser
  JavaScript never receives OpenEMR tokens."
- Fetching that target directly (`curl -sk https://chat.localhost/oauth/launch`,
  no cookies sent) returns `302` to
  `https://emr.localhost/oauth2/default/authorize?response_type=code&client_id=...&code_challenge=...`.
  That target is OpenEMR's own interactive authorize/consent page (rendered HTML,
  reached by full-page browser navigation the same way any OAuth "Login with X"
  button works) — not a background XHR/fetch call carrying a bearer token, and not
  one of the REST/FHIR data endpoints (`/apis/default/api`, `/apis/default/fhir`)
  this project's `ENDPOINT_MATRIX.md` tracks. No OpenEMR REST or FHIR API endpoint
  is called at any point between the iframe rendering and this redirect.
- Separately, and out of this ticket's scope but noted for whichever ticket owns the
  AI server's OAuth client config: the scope string on that redirect is
  `openid api:oemr user/patient.crus user/appointment.cruds` — `user/*` scopes, not
  `patient/*`. That is a pre-existing property of the AI server's registered OAuth
  client (`deploy/local/.env`), unrelated to and unmodified by this probe; flagging
  it here only because it surfaced while confirming this callback, not as a TICK-002
  finding.

### Cleanup performed

- `DELETE FROM modules WHERE mod_directory='aeai-portal-hook-proof'`.
- `rm -rf interface/modules/custom_modules/aeai-portal-hook-proof` inside the
  container.
- Re-ran the login → home.php sequence: login still succeeds and `home.php` still
  returns `200`, with the marker now absent, confirming the environment was left in
  a working state for later tickets (TICK-012 in particular).
- No container restart was performed or needed at any point in this probe.

## Rejected alternatives

Unchanged from the prior pass — see git history for this file. Briefly: direct
SMART-on-FHIR test launches (global is off by default, not an established
patient-facing path) and patching portal templates in place (not an extension
point, would not survive an upgrade).
