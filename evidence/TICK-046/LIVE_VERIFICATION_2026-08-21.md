# TICK-046: live verification of the blocked-breakout fallback, 2026-08-21

TICK-045 broke the OAuth2 login/consent page out of the AI Chat panel's iframe
with `window.top.location.href = window.location.href`. TICK-046 adds a
fallback for the case where the browser silently refuses that top-level
navigation. Both cases below were run in **real Chrome** (Chrome for Testing
141, headless, driven over the DevTools protocol from Node -- no new project
dependency; the harnesses are checked in next to this file and re-runnable).

Everything here tests the *shipped* script: both harnesses extract the
`<script>` body verbatim out of
`openemr_overrides/templates/oauth2/oauth2-login.html.twig` at runtime rather
than restating it, so they cannot pass against a copy that has drifted from
what the container serves. `scope-authorize.html.twig` carries a byte-identical
block, enforced by `ai_server/tests/test_oauth2_breakout_override.py`.

## Part 1 -- real app, real topology (`verify_breakout_fallback_live_app.mjs`)

Runs against the running local stack: a real portal host page embeds the real
`https://chat.localhost/oauth/launch` iframe, whose redirect chain lands on the
real OpenEMR OAuth2 login page.

The running `local-openemr-1` bind-mounts the OAuth2 overrides from the **main
repo checkout**, not from this build worktree, so it still serves the
pre-TICK-046 script. Rather than `docker compose up` from inside a worktree --
the dangling-bind-mount hazard recorded in
`evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`, which took the container
down once already -- the harness intercepts the real login response with
`Fetch.requestPaused` and swaps the old script for this worktree's patched one.
Nothing on the host, in the container, or in the main checkout was modified.
Interception is confirmed live: the patched response is re-paused on its way
through and logged as `breakout script present: true` with the old snippet gone.

```
### real-app case "normal": host page https://emr.localhost/portal/index.php?site=default
    intercepted + patched real login response: https://emr.localhost/oauth2/default/provider/login
    top frame is now: https://emr.localhost/oauth2/default/provider/login
    title: "OpenEMR Authorization"
PASS  real app / same-origin: the breakout still navigates the top frame to the login page
PASS  real app / same-origin: patched login page was the one served -- 2 patched response(s)
PASS  real app / same-origin: no fallback shown -- the normal path is unchanged
PASS  real app / same-origin: a real full-page Sign In form is present
    screenshot: evidence/TICK-046/screenshot-normal-breakout-top-level.png

### real-app case "blocked": host page https://chat.localhost/health
    intercepted + patched real login response: https://emr.localhost/oauth2/default/provider/login
    top frame is now: https://chat.localhost/health
PASS  real app / cross-origin: Chrome silently refused the top-level navigation
PASS  real app / cross-origin: patched login page was the one served -- 3 patched response(s)
    login frame reports: {"text":"Click here to sign in","href":"https://emr.localhost/oauth2/default/provider/login",
                          "target":"_top","display":"block","visibility":"visible",
                          "rect":{"x":0,"y":0,"width":480,"height":54.390625},
                          "stillEmbedded":true,"signInFormBehindIt":true}
PASS  real app / cross-origin: the fallback is visible in the trapped login panel
    screenshot: evidence/TICK-046/screenshot-blocked-fallback-visible.png
    after clicking the fallback at (280, 147.1953125): https://emr.localhost/oauth2/default/provider/login
PASS  real app / cross-origin: clicking the fallback escapes the panel to a full-page sign in
    screenshot: evidence/TICK-046/screenshot-blocked-after-fallback-click.png

8/8 checks passed
```

The blocked case is *exactly* the cross-origin topology TICK-045's own live
verification hit by accident (host page on `chat.localhost`, login page on
`emr.localhost`) and is the deterministic reproduction this ticket asked for:
Chrome refuses the gesture-less top-level navigation with no error and no
user-visible signal. `screenshot-blocked-fallback-visible.png` is the payoff --
the Sign In form still trapped in the panel, exactly as in the original bug,
now with a red "Click here to sign in" banner across the top of it. The click
is dispatched through Chrome's own input pipeline (`Input.dispatchMouseEvent`),
so it carries genuine user activation, which is what makes the `target="_top"`
navigation permitted where the scripted one was not:
`screenshot-blocked-after-fallback-click.png` shows the resulting full-page
Sign In with the host page and the panel gone.

## Part 2 -- isolated origin matrix (`verify_breakout_fallback.mjs`)

Part 1's blocked case depends on Chrome's framebusting policy, so the same
behavior was also pinned down without OpenEMR in the picture: one throwaway
HTTP server reached over two hostnames (`localhost` vs `127.0.0.1` -- two
origins, same way `emr.localhost` and `chat.localhost` are), serving a host
page and an embedded page carrying the shipped script.

```
PASS  blocked: embedded page loaded inside the iframe
PASS  blocked: Chrome silently refused the top-level navigation
PASS  blocked: a visible, actionable fallback appeared -- "Click here to sign in" 420x54.390625px
PASS  blocked: clicking the fallback navigates the top frame to the sign-in page
PASS  blocked: the embedded panel is gone -- sign-in is now the whole page
PASS  normal: the same-origin breakout still navigates the top frame
PASS  normal: no fallback was ever shown to the patient
PASS  normal: sign-in page is top-level, host page gone

8/8 checks passed
```

The "no fallback was ever shown" check is the regression guard for the normal
path: the fallback timer is cancelled on `pagehide`, so a breakout that *did*
work never flashes the banner on its way out. The embedded page reports what it
observed back to the harness server on a 100ms poll, so a fallback that
appeared even briefly would have been caught.

## Part 3 -- the patched templates are still valid Twig

The change is JS inside an existing `{% block scripts %}`, so the one
template-level risk is a stray Twig token. Checked against the real container's
own Twig, compiling the patched files together with the vendor
`oauth2-base.html.twig` they extend (copied to the container's `/tmp`, which is
not a bind mount, so nothing outside this worktree was touched):

```
$ docker cp openemr_overrides/templates/oauth2/oauth2-login.html.twig    local-openemr-1:/tmp/tick046-login.html.twig
$ docker cp openemr_overrides/templates/oauth2/scope-authorize.html.twig local-openemr-1:/tmp/tick046-scope.html.twig
$ docker cp evidence/TICK-046/twig_lint.php local-openemr-1:/tmp/tick046_lint.php
$ docker exec local-openemr-1 php /tmp/tick046_lint.php
OK      tick046-login.html.twig (tokenize + parse + compile, incl. oauth2-base.html.twig)
OK      tick046-scope.html.twig (tokenize + parse + compile, incl. oauth2-base.html.twig)
```

## Limits of this pass, stated plainly

- Chrome only. The fallback is an `<a target="_top">` and a `setTimeout`, so
  there is nothing browser-specific in it, but Safari/Firefox were not run.
- The 1500ms threshold is a heuristic. A same-origin top-level navigation that
  takes longer than that to *commit* while the panel document stays alive would
  briefly show the banner before navigating away anyway; `pagehide` cancels it
  at unload, and in every run above the normal path never rendered it.
- Part 1 injects the patched script into the real response rather than
  rebuilding the container from this worktree. Once merged, the container
  serves these bytes from the bind mount with no interception -- and per the
  TICK-045 process note, the affected container should be recreated from the
  **main repo checkout** after merge.
