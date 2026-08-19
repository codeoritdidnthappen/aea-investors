# TICK-002 portal-hook evidence

## Result

**Blocked:** the available browser automation surface returned `No browser is
available`. The required authenticated browser login, iframe launch, logout, and
network capture therefore could not be performed. This record preserves the
completed source and disposable-stack checks; it is not evidence that the
interactive acceptance criterion passed.

## Pinned release and supported hook

The probe used `openemr/openemr:8.3.0`, resolved locally as
`openemr/openemr@sha256:f2fba00bd7f4c9220ce996bc0876786119f405bcfe2d065b811c31cf32e32a92`.

The selected hook is the patient-portal render event:

```php
OpenEMR\Events\PatientPortal\RenderEvent::EVENT_SECTION_RENDER_POST
// event name: home.section.render.post
```

In the pinned source, `src/Events/PatientPortal/RenderEvent.php` identifies it
as output after the portal SPA sections render. `portal/home.php` supplies that
event to the portal page. The bundled `oe-module-comlink-telehealth` custom
module subscribes to the same hook in
`src/Controller/TeleHealthPatientPortalController.php`, which demonstrates
that it is an upstream-supported patient-portal extension point rather than a
template patch.

## Installation path

Install a custom module under:

```text
interface/modules/custom_modules/<module-directory>/
```

with an `openemr.bootstrap.php` entrypoint, then enable it through OpenEMR's
module management path. The pinned `src/Core/ModulesApplication.php` loads
active custom modules (`modules.type = 0`, `modules.mod_active = 1`) and calls
their bootstrap. The disposable probe placed a minimal module at
`aeai-onboarding-proof`, registered it with `mod_active = 1`, and restarted the
container; this is evidence for the loading contract only, not a recommended
production installation procedure.

## Disposable synthetic probe

The temporary custom-module listener emitted this iframe after the portal
render event:

```html
<section id="aeai-onboarding-launch">
  <iframe title="AI onboarding" src="http://127.0.0.1:8000/health"></iframe>
</section>
```

The source deliberately includes no OpenEMR API URL, bearer token, `fetch`, or
`XMLHttpRequest` call. Its only browser-visible destination is the separate AI
server health endpoint. The final module must replace this health probe with
the AI-server OAuth launch endpoint and retain the same browser boundary.

Observed logged-out behavior from the local OpenEMR portal endpoint:

| Action | Result |
| --- | --- |
| `GET /portal/home.php` without a portal session | `302 Found` to `index.php?site=&w` and an `HttpOnly; SameSite=Strict` portal cookie |
| Enable the disposable module | `modules` row `aeai-onboarding-proof`, `mod_active=1`, `type=0` |
| Browser login → iframe launch → logout → network capture | **Not run:** no browser surface was available |

No patient or production data was used. The temporary database volume, module,
and containers are removed after this probe.

## Required rerun

When browser control is available, run against a fresh synthetic patient with
portal access and attach a redacted HAR or equivalent showing:

1. the logged-out redirect above and absence of the iframe;
2. successful portal login and the iframe under `#aeai-onboarding-launch`;
3. a browser request only to the AI-server launch/callback origin, with no
   `/apis/`, `/oauth2/`, or other OpenEMR API request initiated by the iframe;
4. logout followed by the logged-out redirect and no iframe.
