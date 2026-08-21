# TICK-045: independent live verification, 2026-08-21

Build-agent's own `evidence/TICK-045/FIX_VERIFICATION.md` explicitly noted a
gap it could not close: it verified the breakout `<script>` is present in
both templates' delivered HTML (via `curl`/cookie-jar, no browser tool
available to it in that session), but not that a real browser actually
*executes* the `window.top !== window.self` breakout and navigates away.
This picks up exactly that gap, using real desktop Chrome, before accepting
this ticket as done -- matching this session's own established discipline of
never trusting a build-agent ticket without independent live re-verification.

## Pre-check: recovered a broken container left by build-agent's own verification pass

Before testing, `docker exec local-openemr-1 md5sum .../oauth2-login.html.twig`
intermittently returned `No such file or directory` despite `ls`/`stat`/`find`
succeeding on the same path. Attempting `docker restart local-openemr-1` to
clear this surfaced the real cause:

```
Error response from daemon: Cannot restart container local-openemr-1: ...
failed to fulfil mount request: open /host_mnt/Users/megalodon/src/aea-investors/
.builder/worktrees/TICK-045/openemr_overrides/templates/oauth2/oauth2-login.html.twig:
no such file or directory
```

Build-agent's own `docker compose up -d openemr` (part of its "Live
verification" section) ran from inside its own ephemeral git worktree
(`.builder/worktrees/TICK-045/`), so the relative bind-mount path in
`deploy/local/docker-compose.yml` resolved against *that* worktree's copy of
`openemr_overrides/`, not the main repo checkout. Once the worktree was
cleaned up (normal post-merge build-agent behavior), the running container
was left with a dangling mount reference -- reads worked in a stale/cached
way for a while, then the container failed to restart at all once the mount
had to be re-established. **This took `local-openemr-1` down entirely for a
few seconds during recovery.**

Fixed by recreating the container from the main repo checkout instead:

```
$ cd /Users/megalodon/src/aea-investors/deploy/local && docker compose up -d openemr
 Container local-openemr-1 Recreated
 Container local-openemr-1 Started
```

Re-verified healthy, mounts now correct and stable:

```
$ docker compose ps openemr        -> Up, (healthy)
$ docker exec local-openemr-1 md5sum .../oauth2-login.html.twig .../scope-authorize.html.twig
  5a79ada676906b66cd9fd50ae5679717  oauth2-login.html.twig   (matches openemr_overrides/ in this checkout)
  643208ce98d1039ffa52607abc980d26  scope-authorize.html.twig (matches openemr_overrides/ in this checkout)
$ curl -sk https://chat.localhost/health
  {"status":"ok","dependencies":{"ai_server":"ok","openemr_api":"ok","ocr":"ok","external_llm":"ok"}}
$ curl -sk -o /dev/null -w "%{http_code}\n" https://emr.localhost/portal/
  200
```

**Process note for future tickets:** any ticket where build-agent's own build
touches `docker-compose.yml` bind mounts needs its affected container(s)
recreated from the *main repo checkout* (not trusted as-is from whatever
state build-agent's worktree left it in) before relying on or restarting
them -- otherwise the container silently carries a mount pointing at a
worktree that no longer exists, and can fail to restart later with no
warning until someone tries.

## Finding 1 fix: real-browser confirmation

Forced a deterministic expired-session state (no waiting on `session_ttl`)
by clearing every row from the ai-server's own `sessions` table directly:

```
$ docker exec local-ai-server-1 python3 -c "
import sqlite3, os
c = sqlite3.connect(os.environ['AI_SESSION_DATABASE_PATH'])
c.execute('DELETE FROM sessions'); c.commit()"
deleted, remaining: 0
```

This invalidates any `ai_session` cookie already held by the browser,
reproducing "ai-server session expired" without needing to wait out the real
TTL -- same category of technique this ticket's own Testing section
recommended (shortening `session_ttl`), applied via direct state instead.

**First attempt (not representative of production, noted honestly):** built
a test harness page at `https://chat.localhost/` (chat.localhost as the
*top* frame) with an iframe pointing at the real
`https://chat.localhost/oauth/launch` entry point, matching the panel's own
markup. The redirect chain reached `emr.localhost/oauth2/default/provider/login`
(confirmed via network capture) and rendered the Sign In form -- but the
*top* frame did not navigate; the form stayed visually trapped in the
iframe. This did **not** reproduce the fix working.

**Root cause of that first attempt's failure, and the corrected test:** in
real production, the portal host page embedding this iframe is itself
served from `emr.localhost` (`portal/home.php`), not `chat.localhost`. Once
`chat.localhost/oauth/launch`'s redirect chain lands on the login page, that
page is **same-origin** with the real parent (`emr.localhost`). My first
test's harness page was hosted on `chat.localhost` instead, making the
breakout a *cross-origin* top-navigation attempt -- which Chrome's own
framebusting protections silently restrict without a user gesture, unlike
the real same-origin case. Rebuilt the harness on `https://emr.localhost/portal/index.php`
(matching the real parent origin) and repeated the identical injection:

```
$ navigate tab -> https://emr.localhost/portal/index.php?site=default
$ inject: <div id="aeai-portal-chat"><iframe src="https://chat.localhost/oauth/launch" style="min-height:640px"></iframe></div>
```

Result: the tab's own title and URL changed to
`"OpenEMR Authorization" / "https://emr.localhost/oauth2/default/provider/login"`,
and the screenshot shows a full top-level Sign In page with no trace of the
injected host page (no red panel border, no "Portal host page" marker text
visible) -- **the breakout fired and the tab genuinely navigated to the
login page as a normal top-level document, exactly as this ticket's fix is
supposed to do, confirmed in the real topology.**

This closes the specific gap build-agent's own evidence flagged as untested
("that a real browser executing this script actually navigates
`window.top`"). Both `oauth2-login.html.twig` and `scope-authorize.html.twig`
carry the identical breakout script wired into the same `oauth2-base.html.twig`
`scripts` block (confirmed by reading `oauth2-base.html.twig` directly: the
block renders unconditionally in `<head>`, before `<body>`); having confirmed
real execution on one, and confirmed the markup/wiring is identical on the
other via source review, both are treated as verified.

## Findings 2 and 3: independently corroborated, no new testing needed

Re-read `AuthorizationController.php`'s `updateAuthRequestWithUserApprovedScopes()`
and the full, current `scope-authorize.html.twig` line by line (both already
open from this ticket's own investigation). Confirmed directly: every
`name="scope[...]"` field the real form can submit -- static and the
JS-reconstructed dynamic ones alike -- sets `value` to the scope string
itself, never the bare `"1"` that triggers the logged error. This
independently corroborates build-agent's own Finding 3 triage (harmless log
noise from non-JS-driven submissions, not reachable by a real
Authorize click). Finding 2 (`persist.php` 503)'s triage -- log review plus a
live 5/5-success re-test -- is sound methodology and not re-run here; no
`503` was observed anywhere in this pass's own testing either.

## Session-wide side effect, on the record

Clearing the `sessions` table logged out every ai-server session that
existed in this environment at the time (this session's own accumulated
test sessions from earlier tickets). Expected and reversible -- normal
re-login/re-authorization restores access; no data loss.
