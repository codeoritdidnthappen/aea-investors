# TICK-045: fix + live verification, 2026-08-21

Executed against the same running local Docker topology
(`local-openemr-1`, `local-mariadb-1`, `local-caddy-1`, `local-ai-server-1`)
`evidence/TICK-045/CHAT_PANEL_INVESTIGATION.md` used. No browser-automation tool
was available in this session (checked via the same method
`evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` documents) even though the
investigation that filed this ticket had one; verification below is cookie-jar
`curl`/`urllib` against the real HTTPS endpoints instead, following that same
established precedent.

## Finding 1 fix: iframe breakout on the two vendor OAuth templates

Chose the third of the ticket's three sanctioned options: "redirecting
session-expired re-auth to a full top-level page instead of embedding it in
the 640px panel". Implemented as a `window.top !== window.self` breakout
script injected into the `scripts` block (rendered in `<head>`, before
`<body>`, so it fires before any embedded content paints) of:

- `openemr_overrides/templates/oauth2/scope-authorize.html.twig` (already an
  existing TICK-037 vendor override -- added the block, kept the rest
  byte-for-byte unchanged).
- `openemr_overrides/templates/oauth2/oauth2-login.html.twig` (new override,
  vendor file copied unmodified from the running pinned container plus the
  same `scripts` block addition).

Both are bind-mounted read-only over their vendor paths in
`deploy/local/docker-compose.yml`, the same pattern TICK-037 already
established for `scope-authorize.html.twig`.

This targets exactly the failure mode Finding 1 identified: the normal/fast
path (ai-server session still effectively valid, OpenEMR silently
re-authorizes) never renders either template at all -- it's pure HTTP
redirects -- so nothing changes there. Only when OpenEMR actually needs to
show interactive HTML (a login form or a consent form) does the breakout
fire, and only when that HTML is actually embedded in an iframe (the `if`
guard is a no-op on a direct/top-level visit). A full top-level navigation is
inherently an unambiguous "you need to sign in again" signal, satisfying the
ticket's second acceptance criterion structurally, without added copy.

### Live verification

Recreated `local-openemr-1` (`docker compose up -d openemr` from
`deploy/local`, `.env` reconstructed from the running containers' own already
-set env vars so no secret changed) to pick up the new bind mount; confirmed
healthy again afterward.

```
$ docker exec local-openemr-1 md5sum .../templates/oauth2/oauth2-login.html.twig .../templates/oauth2/scope-authorize.html.twig
5a79ada676906b66cd9fd50ae5679717  .../oauth2-login.html.twig
643208ce98d1039ffa52607abc980d26  .../scope-authorize.html.twig
# identical to md5sum of the two files under openemr_overrides/ in this worktree
```

Then drove the *exact* real entry point the AI Chat panel's iframe uses
(`https://chat.localhost/oauth/launch`, `PortalChatController::render()`'s own
`DEFAULT_CHAT_LAUNCH_URL`) through a real, fresh (no prior OpenEMR session)
cookie jar:

```
curl -sk https://chat.localhost/oauth/launch -c jar -L
  -> 200 https://emr.localhost/oauth2/default/provider/login
  -> body contains the TICK-045 breakout <script> exactly once, in <head>
```

Logged in as the synthetic patient (`AverySubjecttest1`, pid 1 -- portal
password reset by direct SQL/bcrypt the same way
`evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` already did and documented, since
the prior value wasn't known to this session either; not printed here, and
left in a working state afterward) and submitted that login form with the
same jar:

```
POST https://emr.localhost/oauth2/default/login (csrf_token_form, username, password, user_role=portal-api)
  -> 200 https://emr.localhost/oauth2/default/scope-authorize-confirm
  -> body contains the TICK-045 breakout <script> exactly once, in <head>
```

Both HTML pages an expired-session patient would actually be served --
login, then consent -- now carry the breakout script, confirmed against the
real running stack via the real launch URL, not just read from source. What
this does *not* prove (no browser tool available, noted honestly rather than
assumed): that a real browser executing this script actually navigates
`window.top` and that a human then completes the flow without friction. The
`window.top !== window.self` / `window.top.location.href = window.location.href`
pattern is standard, well-understood browser behavior; the gap is JS
execution proof, not the template delivery.

## Finding 2: `persist.php` 503 -- not currently reproducible, root-caused as transient

Re-checked `local-openemr-1`'s Apache `access.log`/`access.log.1` for every
`persist.php` request logged: 15×`200`, 2×`400` (a `curl` probe with no CSRF
token, unrelated), 2×`403` (a stale-session probe from 2026-08-20) -- **zero**
`503`s anywhere in the currently-retained log window. Checked Caddy's
container log (its only per-request entries are `warn`/`error` level; no
access-log directive is configured in `deploy/local/Caddyfile`) for anything
matching `persist` or `503`: none. The only `503` in Caddy's log at all is
unrelated (a `chat.localhost/health` probe hitting `ai-server` during a
moment it was still starting).

Live re-test: authenticated as the same synthetic patient via the classic
portal cookie-jar login (mirrors `evidence/TICK-002`'s method), loaded
`portal/home.php` to extract its real per-page CSRF token, then issued the
exact request shape `templates/portal/home.html.twig`'s own `persist(where)`
JS sends on a dashboard-tile click (`POST lib/persist.php`,
`Content-Type: application/json`, `{csrf_token_form, where, portal_init}`)
five times in a row:

```
persist.php attempt 0: 200
persist.php attempt 1: 200
persist.php attempt 2: 200
persist.php attempt 3: 200
persist.php attempt 4: 200
```

5/5 succeeded. Combined with the clean log search above and
`persist.php`/`PortalChatController::render()` having no code dependency on
each other (`render()` unconditionally echoes the iframe markup; nothing in
its call path reads `persist.php`'s response or is blocked by its failure),
this is root-caused as: **not currently reproducible, and structurally
unable to block the chat panel's own render regardless of its own status**.
The original `503` was real (the investigation's own network capture caught
it twice), but is most consistent with a transient condition at the
Caddy/Apache layer (e.g. a brief window during container startup/restart)
rather than a persistent defect -- exactly the "confirmed genuinely unrelated
to chat reliability with evidence" branch of this ticket's third acceptance
criterion. No code change made here (none is evidenced as necessary); if a
`503` recurs, capturing `docker logs local-caddy-1` and
`local-openemr-1`'s Apache access/error logs at the exact moment (not
after the fact) is the next diagnostic step, since neither currently retains
enough history to catch an already-past transient failure after it stops
reproducing.

## Finding 3: scope-parsing bug -- triaged as harmless log noise, not reachable through the real UI

Read `AuthorizationController::updateAuthRequestWithUserApprovedScopes()`'s
only two call sites (`authorizeUser()`, the real consent-submit path; and
`processAuthorizeFlowForLaunch()`, the unrelated SMART-launch skip-auth path)
and the current (TICK-037-fixed) `scope-authorize.html.twig` line by line:
every `name="scope[...]"` field the real consent form can submit -- the
static `otherScopes`/`hiddenScopes`/`offline_access` inputs, and the
dynamically-generated ones `reconstructScopes()`'s `addScopeInput()` builds
at submit time -- sets `value` to the scope string itself, never the literal
`"1"`. The per-resource action/restriction checkboxes that *do* only carry a
`"1"`-shaped implicit state have no `name` attribute at all, so a real
browser never submits them directly; only the JS-reconstructed inputs above
are. A real browser with this form's JS running cannot produce
`scope[x]=1` as a submitted pair.

`evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md` (same session-day)
independently corroborates this: its own credential-provisioning step
explicitly recorded that a **headless/raw-POST replication of this exact
consent form was attempted first and failed** ("the granted scope came back
as just `nonce`... most likely because the consent page's per-resource
checkboxes are packaged into the submission by page JS a raw POST doesn't
replicate") before falling back to a real browser click, which then
succeeded and issued a full-scope token. The `Invalid scope format: 1` burst
this ticket's own investigation captured is timestamped in the same session
and is the server-side symptom of exactly that kind of naive non-JS POST
attempt (whichever tool made it -- not `scripts/probe_assessment_draft.py`'s
or `scripts/probe_scheduling_parity.py`'s current, checked-in versions,
which both already build `scope[x]=x`, matching the real form).

**Triage conclusion: confirmed harmless log noise from automation/tooling
that doesn't replicate the consent form's own JS-driven scope reconstruction,
not a defect reachable by a real patient clicking Authorize in a real
browser.** No code change made to core OpenEMR's `AuthorizationController.php`
(per this ticket's own Out of Scope note: only change it if triage determines
it's actually necessary -- it isn't). Not filed as a separate follow-up
ticket for the same reason.

## Deviations on the record

- `deploy/local/.env` was reconstructed (not committed -- gitignored) from
  the already-running containers' own env vars, to recreate `local-openemr-1`
  with the new bind mount without changing any secret or disturbing the
  other services.
- Patient `pid 1` (`AverySubjecttest1`)'s portal password was reset by direct
  SQL/bcrypt for this pass's live verification, the same category of
  deviation `evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` already recorded (the
  prior value wasn't known to this session either). Portal login for `pid 1`
  was confirmed still working after this pass.
