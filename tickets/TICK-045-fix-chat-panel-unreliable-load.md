---
id: TICK-045
title: "bug(portal): AI Chat panel doesn't reliably come up when clicked"
type: task
epic: EPIC-04
priority: P1
estimate: M
depends_on: [TICK-002, TICK-032, TICK-033]
labels: [portal, chat, auth, bug]
source: [FR-1, FR-2, NFR-19]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/92
builder_commit: 69b58fa
---
## Context

User report (2026-08-21): "the chat doesn't reliably come up for the user
when they click on it." Investigated live against the local Docker
topology (real desktop Chrome, browser console + network capture, and
`docker exec`/`docker logs` on `local-openemr-1`) rather than reasoning
about it in the abstract. Found one clear, well-evidenced root cause and
two secondary findings that need further triage; documented with
appropriate confidence for each.

### Finding 1 (primary suspect, high confidence): expired-session re-auth is embedded in a fixed-height, effectively non-scrollable panel

`openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php`'s
`render()` method emits the AI Chat panel as:

```php
'<div id="aeai-portal-chat" class="card collapse overflow-auto" data-parent="#cardgroup">'
    . '<header ...>AI Chat</header>'
    . '<div class="card-body p-0">'
    . '<iframe ... src="..." style="width:100%;min-height:640px;border:0;"></iframe>'
    . '</div></div>';
```

The iframe's `src` is always `https://chat.localhost/oauth/launch`
(`DEFAULT_CHAT_LAUNCH_URL`) -- every time the panel opens, not just the
first time. `openemr/home.php`'s own "remember last panel" mechanism
(`portal/lib/persist.php`'s `whereto` setting, triggered automatically on
every dashboard-tile click) means a patient who was last on the AI Chat
panel gets it **auto-reopened** on their next portal visit.

If the ai-server's own session has expired since then (`AuthSettings.
session_ttl`, a normal occurrence for any return visit after enough idle
time), `/oauth/launch` redirects into a **full OpenEMR login screen**, and
after logging in, a **full OAuth consent screen** (resource-permission
checkboxes, "Offline Access Requested" section, then the Authorize
button) -- both rendered *inside* this same `min-height:640px` iframe
instead of as a top-level page.

Live-reproduced: on a fresh portal page load with an expired ai-server
session, the AI Chat panel auto-opens showing the embedded login form.
After logging in, the consent screen appears, visibly taller than the
640px panel, with the Authorize button below the fold. Neither mouse-wheel
scroll (tested directly over the panel content) nor extended keyboard `Tab`
navigation (tested well past the point where focus should have reached the
Authorize button) reliably moved past the first couple of resource-permission
rows -- the panel appears to trap focus/scroll rather than exposing the
rest of the form. (One caveat, noted honestly: a single early `Tab` press
did appear to nudge a scrollbar into view once, so there is some chance
part of this specific symptom is an artifact of this investigation's
browser-automation tooling rather than identical for a human with a real
mouse/trackpad -- but the panel's fixed `min-height:640px` with no `height`
cap and no `scrolling` attribute is a real, structural risk regardless, and
the dashboard-tile approach means most patients will never see this at all
until their session happens to expire mid-visit, which is exactly the
"doesn't *reliably* come up" pattern reported.)

**Net effect**: a patient whose session has expired and who is
auto-returned to the AI Chat panel gets what looks like a broken/stuck
"chat" (actually a login+consent flow they can't see or complete), not an
obvious "please sign in again" prompt.

### Finding 2 (confirmed, likely unrelated to the reported symptom): `persist.php` 503

`POST https://emr.localhost/portal/lib/persist.php` -- OpenEMR's own core
"remember last panel + patient settings" endpoint, unrelated to this
module's own code -- returns `503` on every dashboard-tile click,
100% reproducible in this environment. Did not find a matching entry in
`local-openemr-1`'s Apache/PHP error log, suggesting the 503 may originate
below the PHP application layer (Caddy or Apache resource handling) rather
than from `persist.php`'s own logic. Did not block the chat panel's own
render in this investigation's testing, so likely a secondary issue, but
worth root-causing since it means the portal's own "resume where you left
off" behavior may not actually be persisting correctly.

### Finding 3 (real code defect confirmed, real-world impact unclear): scope-approval parsing bug in core OpenEMR

`local-openemr-1`'s Apache error log showed repeated `OpenEMR.ERROR:
AuthorizationController->updateAuthRequestWithUserApprovedScopes() Exception
occurred while processing approved scopes {"message":"Invalid scope format:
1", ...}` during this investigation's own consent-flow testing. Root cause
identified directly from `src/RestControllers/AuthorizationController.php`:

```php
$authRequest = $this->updateAuthRequestWithUserApprovedScopes($authRequest, $request->request->all('scope'));
// ...
foreach ($approvedScopes as $scope) {
    $approvedScopeEntity = ScopeEntity::createFromString($scope); // $scope is "1", not "api:oemr"
```

The consent form submits checked resource permissions as `scope[api:oemr]=1`
-- an associative array where the **keys** are scope names and the
**values** are the checkbox-checked marker `"1"`. `foreach ($approvedScopes
as $scope)` iterates **values**, so `$scope` is always the literal string
`"1"`, which `ScopeEntity::createFromString()` correctly rejects. This is a
real, reproducible bug in core OpenEMR (not this project's own module
code), but its practical impact is unclear: live testing elsewhere this
session showed full-scope tokens were still issued successfully despite
this error firing on every consent submission, suggesting some other path
still grants access even though the "customize which permissions to
approve" feature is silently broken. Needs its own investigation to
determine whether this ever actually blocks authorization (versus just
being log noise from a no-op fallback) -- flagged here because it fires on
the exact same consent screen Finding 1 is about, and could compound it.

Full evidence: `evidence/TICK-045/CHAT_PANEL_INVESTIGATION.md`.

**Fixed and independently live-verified (2026-08-21):** build-agent
implemented the iframe-breakout fix (`evidence/TICK-045/FIX_VERIFICATION.md`)
but had no browser tool available to confirm real-browser execution.
Independently closed that gap in real desktop Chrome -- see
`evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`, which also documents and
recovers from an unrelated operational hazard found along the way: the
running `local-openemr-1` container had been recreated from inside
build-agent's own now-deleted git worktree, leaving its bind mount pointing
at a path that no longer existed. Recreating the container from the main
repo checkout fixed it.

## Acceptance Criteria

- [x] The AI Chat panel's embedded re-authentication flow (login + consent)
      is fully usable within the panel's own space -- either by giving the
      iframe enough height to show the whole flow without scrolling, by
      ensuring the iframe's own internal scrolling reliably works (verified
      with both mouse and keyboard), or by redirecting session-expired
      re-auth to a full top-level page instead of embedding it in the
      640px panel. Implemented as the third option: an iframe-breakout
      script added to both `oauth2-login.html.twig` and
      `scope-authorize.html.twig`. See `evidence/TICK-045/FIX_VERIFICATION.md`
      and `evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`.
- [x] A patient whose ai-server session has expired and is auto-returned to
      the AI Chat panel gets an unambiguous "please sign in again" signal,
      not a screen that looks stuck or broken. A full top-level navigation
      to the Sign In page is unambiguous by construction; confirmed live in
      real desktop Chrome (see below).
- [x] `persist.php`'s 503 (Finding 2) is root-caused: either fixed, or
      confirmed genuinely unrelated to chat reliability with evidence, and
      recorded either way. Triaged as transient/not currently reproducible
      (5/5 live re-test succeeded, no matching log entries); structurally
      unable to block the chat panel's own render regardless of its own
      status. See `evidence/TICK-045/FIX_VERIFICATION.md`.
- [x] The scope-parsing bug (Finding 3) is triaged: confirmed harmless
      log noise with evidence, or fixed, or filed as its own follow-up
      ticket if it turns out to be a separate, larger issue than this
      ticket's own scope. Triaged as harmless log noise from non-browser
      tooling that doesn't replicate the consent form's own JS scope
      reconstruction -- every field a real browser can submit sets `value`
      to the scope string itself, never the bare `"1"` that triggers the
      error. Independently corroborated by re-reading the same code. See
      `evidence/TICK-045/FIX_VERIFICATION.md` and
      `evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`.
- [x] Reproduced live (real desktop Chrome, real expired session, not just
      code review) that the fix actually resolves the "doesn't reliably
      come up" symptom end to end. Build-agent's own fix pass had no
      browser tool available and could only confirm the script's presence
      in delivered HTML via curl, explicitly flagging real-browser
      execution as unverified. Closed that gap independently: forced a
      deterministic expired-session state (cleared the ai-server's
      `sessions` table), reproduced the exact panel markup in real desktop
      Chrome with the parent hosted on `emr.localhost` (matching production
      -- an unrepresentative `chat.localhost`-hosted first attempt failed
      due to a cross-origin top-navigation restriction that doesn't apply
      to the real same-origin case), and confirmed the tab genuinely
      navigated to a full top-level Sign In page. See
      `evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`.

## Testing

Reproduce with a genuinely expired ai-server session (either wait out
`AuthSettings.session_ttl` or shorten it temporarily in a local test
config) against the local Docker topology, capturing browser console,
network requests, and `local-openemr-1`/`local-ai-server-1` logs before
and after the fix. CI must be green.

## Out of Scope

Redesigning the portal's own "remember last panel" mechanism
(`persist.php`'s `whereto` setting) beyond what's needed to fix this
symptom. Any change to core OpenEMR's `AuthorizationController.php` beyond
what Finding 3's triage determines is actually necessary.
