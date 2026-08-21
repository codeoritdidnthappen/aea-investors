# TICK-045: "chat doesn't reliably come up" investigation, 2026-08-21

Live investigation against the local Docker topology (`local-openemr-1`,
`local-mariadb-1`, `local-caddy-1`, `local-ai-server-1`), real desktop
Chrome, real synthetic patient (`AverySubjecttest1`). Browser console,
network requests, and server-side logs captured throughout, not just code
review.

## Reproduction steps that surfaced Finding 1

1. Logged into the OpenEMR patient portal, opened the AI Chat panel once
   (normal use, LLM chat worked -- confirmed real content from earlier
   testing this session still visible: "I want to udpate my address" /
   "Scheduling assistance is unavailable...").
2. Clicked Dashboard -> AI Chat repeatedly within the *same* already-loaded
   portal page: panel toggled open/closed instantly every time, showing the
   same cached conversation. Not representative of a fresh visit -- the
   panel is a `.collapse` accordion card, not a fresh iframe load, on
   repeat clicks within one page load.
3. **Fresh page load** (`navigate` to `/portal/home.php` again, a new tab):
   the AI Chat panel was **already expanded** (not the dashboard tiles),
   showing a full "Sign In" form rendered inside the 640px-tall panel --
   confirming the portal's own "resume last panel" behavior
   (`persist.php`'s `whereto` setting) combined with an expired ai-server
   session.
4. Logged in inside that embedded form. **First attempt failed** with
   "Sorry, verify the information you have entered is correct" despite
   verified-correct, freshly-typed credentials (screenshotted before
   submit). **Second attempt with the same credentials succeeded.** Not
   fully root-caused -- noted as an open question in the ticket, possibly a
   stale CSRF token on the very first embedded-panel load.
5. After successful login, the OAuth consent screen ("Authorizing for
   Application Intake Assistant...", resource-permission checkboxes,
   "Offline Access Requested" section, Authorize button) rendered inside
   the same 640px panel -- visibly taller than the panel, Authorize button
   below the fold.
6. Mouse-wheel scroll directly over the panel content: **no visible
   movement**, tested twice.
7. Keyboard `Tab` (25 presses, then 40 more): first batch appeared to shift
   a scrollbar into view once; the second, larger batch produced **no
   further visible change at all** -- inconsistent with reliably reaching
   the Authorize button through normal keyboard navigation either.
8. Did not force a workaround (e.g. resizing the window, zooming out) to
   artificially complete the flow, since a real patient wouldn't
   necessarily know to try that either -- the point being tested was
   whether *normal* interaction reaches the button, not whether *any*
   workaround exists.

## Finding 2: `persist.php` 503

Captured via `read_network_requests` on every Dashboard-tile click:

```
POST https://emr.localhost/portal/lib/persist.php -> 503
```

100% reproducible (2/2 clicks captured). Checked `local-openemr-1`'s Apache
error log (`/var/log/apache2/error.log`) for the same time window -- no
matching PHP-level error, suggesting the 503 may originate below the PHP
application (Caddy or Apache), not inside `persist.php`'s own logic.
`persist.php` is core OpenEMR (`portal/lib/persist.php`), unrelated to this
project's own module code.

## Finding 3: scope-parsing bug in core OpenEMR

`local-openemr-1`'s Apache error log, same investigation window:

```
[php:notice] ... OpenEMR.ERROR: AuthorizationController->updateAuthRequestWithUserApprovedScopes()
Exception occurred while processing approved scopes {"message":"Invalid scope format: 1", ...}
```

Repeated in bursts of ~4-5 identical lines, three bursts roughly a minute
apart, timestamped during this session's own consent-flow testing (both
this investigation and, likely, some residual noise from earlier
credential-provisioning work done for TICK-026 in this same session).

Root cause read directly from
`src/RestControllers/AuthorizationController.php:1252,1334-1336`:

```php
$authRequest = $this->updateAuthRequestWithUserApprovedScopes($authRequest, $request->request->all('scope'));
...
foreach ($approvedScopes as $scope) {
    $approvedScopeEntity = ScopeEntity::createFromString($scope);
```

`request->request->all('scope')` returns the submitted
`scope[api:oemr]=1&scope[api:fhir]=1&...` fields as an associative array
(`['api:oemr' => '1', 'api:fhir' => '1', ...]`). `foreach (... as $scope)`
iterates **values** (`"1"`), not **keys** (the actual scope names), so
`ScopeEntity::createFromString("1")` always throws. This is a genuine,
reproducible defect in core OpenEMR's own consent-processing code -- not
this project's module.

**Open question, not resolved in this pass**: despite this error firing on
every consent submission observed, full-scope tokens were still
successfully issued in other testing this session (e.g. TICK-026's
credential provisioning got a token with the complete requested scope
list). This means either (a) there's a fallback path elsewhere that grants
the originally-requested scopes when the "approved subset" ends up empty,
making this bug log noise rather than a functional blocker, or (b) the
successful grants observed elsewhere used a different code path (e.g. a
remembered/pre-approved client) that doesn't hit this function at all. Not
distinguished in this pass -- needs dedicated investigation, called out as
its own AC in the ticket.

## What wasn't tested

A full, controlled before/after comparison with the ai-server session
deliberately expired via a shortened `session_ttl` (rather than relying on
this session's own long elapsed real time to produce a naturally-expired
session) -- would make Finding 1 fully deterministic to reproduce on
demand rather than dependent on session age. Recommended as the first step
for whoever picks up this ticket.
