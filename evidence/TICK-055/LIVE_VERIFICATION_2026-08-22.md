# TICK-055 live verification — 2026-08-22

Signing out of the OpenEMR portal must end the AI chat session, and the previous
patient's session must not be resumable by navigating straight to the chat origin
afterwards. TICK-055's Testing section requires this be shown "against the local Docker
topology with real desktop Chrome … not only by unit test."

**Result: 11/11 checks passed.**

- Driver: `evidence/TICK-055/run_live_verification.sh`
- Capture: `evidence/TICK-055/verify_portal_signout_ends_chat.mjs`
- Both are committed and re-runnable: `bash evidence/TICK-055/run_live_verification.sh`

## Method

Real desktop Chrome (`/Applications/Google Chrome.app`, headless=new, driven over CDP),
real portal login as the seeded synthetic patient (pid 1), real local Docker topology
(Caddy + OpenEMR 8.3.0 + ai-server + MariaDB 11.8.8).

Unlike TICK-054's harness, **this one patches nothing.** `local-openemr-1` bind-mounts
the module from this worktree and `local-ai-server-1` was rebuilt from it, so the markup
and the server behaviour measured here are the real ones. Step 2 of the driver refuses
to proceed unless that is true:

```
== 2. host and container must agree before anything is measured ==
   module PHP matches: 0bd42bda467b873e1d3bef0a1cdf870f
   ai-server has /api/logout
   ai-server AI_SESSION_PORTAL_ORIGIN=https://emr.localhost
   openemr  AEAI_PORTAL_CHAT_LOGOUT_URL=https://chat.localhost/api/logout
```

This is the recorded hazard for this repo: **`ai-server` is not bind-mounted**
(`deploy/local/docker-compose.yml` builds it from context), so without
`docker compose up -d --build ai-server` the run would have measured the previously
built, logout-less image and passed nothing.

Every verdict has two independent witnesses — the browser (a real mouse click on the
portal's own Logout control, a CDP network capture, and real HTTP statuses) and the
server (the `ai_session` SQLite row set read straight out of the ai-server container,
plus a diff of its request log). "The session ended" therefore means *the row holding
the patient's encrypted OpenEMR tokens is gone*, not that a cookie looked different.

## The Origin discipline, straight at the running server

```
   off-origin POST      -> 403  (want 403)
   no-Origin POST       -> 403  (want 403)
   portal-origin POST   -> 204  (want 204)
   chat-origin POST     -> 204  (want 204)
   GET (must not exist) -> 405  (want 405)
   /api/chat w/ portal  -> 403  (want 403)
```

The last line is the one that matters most: widening *logout* to accept the portal
origin did not widen the *chat turn*. An embedding page still cannot drive a patient's
conversation.

## The run

```
ai_session rows before the run: 8

== 1. portal login as the seeded synthetic patient ==
PASS  portal login lands on the dashboard -- https://emr.localhost/portal/home.php

== 2. open the AI Chat panel and sign in (creates the AI session) ==
    oauth step 1: login
    oauth step 2: consent
PASS  the OAuth patient sign-in completes and lands on the chat origin -- https://chat.localhost/
PASS  opening the panel created exactly one AI session -- 1 new row(s); handle_hash=a4ba7956ae3b5676...

== 3. hold a chat turn from the chat origin (session is live) ==
PASS  a chat turn on the live session is accepted (not 401) -- HTTP 200

== 4. sign out of the OpenEMR portal (real click on its Logout) ==
PASS  the portal renders a sign-out link the hook can bind -- ./logout.php
PASS  the sign-out click fires a POST to the AI server logout endpoint -- 1 request(s), method=POST
PASS  the portal itself signed out (left the dashboard) -- https://emr.localhost/portal/index.php?site=default&logout

== 5. the AI session row is gone from the ai-server ==
PASS  the patient's AI session row is deleted, not merely expired -- handle_hash=a4ba7956ae3b5676... present=false
PASS  no other session row was collaterally deleted -- 8 row(s) remain, was 8 before the run

== 6. navigate directly to the chat origin after signing out ==
PASS  the previous patient's session does not resume: /api/chat is 401 -- HTTP 401
PASS  GET / still serves only the empty chat shell, with no transcript -- title=AI Chat

11/11 checks passed

ai-server POST /api/logout: before=4 after=5 delta=1
                  of which 204: before=2 after=3 delta=1
```

Step 6 is the acceptance criterion stated in the ticket's own words — "navigating
directly to the chat origin after signing out of the portal, not only by unit test."
Before this ticket the same navigation resumed chatting as that patient against a live
delegated token.

Screenshots: `screenshot-1-dashboard.png`, `screenshot-2-chat-panel-open.png`,
`screenshot-3-chat-origin-while-signed-in.png`, `screenshot-4-after-portal-signout.png`,
`screenshot-5-chat-origin-after-signout.png`.

## Deviations on the record

- **The seeded patient's portal password is reset immediately before the run** (driver
  step 4), to a known value. Same deviation, same category, as
  `evidence/TICK-054/run_live_verification.sh` and `evidence/TICK-045`.
- **Chrome runs with `--ignore-certificate-errors`** (the local stack is self-signed)
  and `--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks`. Both
  `emr.localhost` and `chat.localhost` resolve to the loopback, so Chrome's Local
  Network Access checks would otherwise block the panel's cross-origin subresource
  before it is sent. A property of this all-on-loopback demo topology, not of the
  product — recorded the same way in `evidence/TICK-054`.
- **The OAuth login and consent forms are submitted by clicking their real named submit
  buttons** (`user_role=portal-api`, `proceed=1`) from script rather than by synthetic
  mouse coordinates. The clicks under test — the AI Chat tile and the portal's Logout
  control — are real hit-tested mouse presses.
- **The run left the stack rebuilt from this worktree.** `local-ai-server-1` and
  `local-openemr-1` were recreated from this branch's code and will stay that way until
  someone rebuilds from `main`.

## An incidental finding: the accumulation is real

The run began with **8 `ai_session` rows already on disk**, every one of them an
abandoned session from earlier verification work, each holding a patient's AES-GCM
encrypted OpenEMR access and refresh tokens. None had ever been swept, because
`purge_expired()` was never called anywhere and the `ai_session` cookie carries no
`max_age` — closing the browser discards the handle, so `active_session`'s lazy delete
never gets the chance to fire. That is the NFR-31 problem AC5 describes, observed in
the wild rather than argued from the code.

All 8 were still inside their 8-hour TTL at the time of the run, so the new startup
sweep correctly left them alone — which is the other half of the property, and is
asserted by `test_ac5_a_still_active_session_survives_the_sweep`.

## What this does NOT prove

- **It does not prove sign-out from a non-dashboard portal page ends the AI session.**
  It does not — see failure mode 1 in `PORTAL_LOGOUT_MECHANISM.md`. The hook script is
  only rendered on `portal/home.php`.
- **It does not prove the delegated OpenEMR token is revoked.** It is not, and cannot
  be: OpenEMR 8.3.0 exposes no `revocation_endpoint`. Recorded as a finding in
  `PORTAL_LOGOUT_MECHANISM.md`, per the ticket's Out of Scope.
- **It does not exercise the 8-hour TTL boundary.** No live run in this ticket drives a
  session across 8 hours; the expiry warning and the never-extended TTL are covered by
  unit tests against an injected clock only.
- **It does not prove the periodic sweep fires in production.** The interval sweep is
  covered by a unit test with a 10 ms interval
  (`test_ac5_the_sweep_keeps_running_after_startup`); the default is 1 hour and no live
  run waited for it. The startup sweep is exercised on every container start.
- **It is a single-browser, single-patient run.** No Firefox/Safari, no concurrent
  patients, no third-party-cookie-blocking profile.
