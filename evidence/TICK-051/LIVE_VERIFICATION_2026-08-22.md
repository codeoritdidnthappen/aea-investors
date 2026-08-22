# TICK-051 live verification — 2026-08-22

Real desktop Chrome (headless=new, CDP-driven), real OpenEMR 8.3.0 portal login as a
seeded synthetic patient, real local Docker topology (Caddy + OpenEMR + ai-server +
MariaDB). `ai-server` rebuilt from this worktree with `--build` before the run.

- Driver: `evidence/TICK-051/run_live_verification.sh`
- Capture: `evidence/TICK-051/verify_signin_lands_on_dashboard.mjs`
- Result: **19/19 browser checks passed**, plus 6 server-side probes in the wrapper.

## The bug, and what changed

Before: `GET /oauth/callback` answered `303 -> AI_SESSION_SUCCESS_REDIRECT_URI`, which
was `https://chat.localhost/`. Since TICK-045's breakout moves the patient to top level
to sign in, that 303 landed them on the standalone chat page full-screen, portal gone.

After: the callback resolves its own position from `Sec-Fetch-Dest` and sends a
top-level authorization to `https://emr.localhost/portal/home.php`, while an in-panel
one is served the chat inline.

## Stale-artifact guards (recorded repo hazard)

`ai-server` is **not** bind-mounted, so the wrapper rebuilds it and then proves host and
container agree by checksum before measuring anything:

```
ai_server/app/main.py matches: 998fae2726cbf600064f0245f93d8fff
ai_server/app/auth.py matches: 1e43a072389b3f09dd279f60136c4f46
portal module PHP matches:     0bd42bda467b873e1d3bef0a1cdf870f
```

Container environment, read from the running container:

```
AI_SESSION_DASHBOARD_REDIRECT_URI=https://emr.localhost/portal/home.php
AI_SESSION_CHAT_ORIGIN=https://chat.localhost
AI_SESSION_SUCCESS_REDIRECT_URI is absent (renamed, not reused)
```

## Server-side probes (wrapper step 4)

| Request | Status | Wanted |
|---|---|---|
| `POST /api/chat`, `Origin: https://chat.localhost` | 401 | 401 — origin accepted, no session |
| `POST /api/chat`, `Origin: https://emr.localhost` | 403 | 403 |
| `POST /api/chat`, `Origin: https://attacker.test` | 403 | 403 |
| `POST /api/chat`, no `Origin` | 403 | 403 |
| `GET /oauth/callback?error=access_denied&…`, `Sec-Fetch-Dest: document` | 303 → `portal/home.php` | 303 → dashboard |

The dashboard origin returning **403** is the specific proof that the split is real: it
is the value the single pre-TICK-051 setting would have taken once the destination was
repointed, and it would have 403'd the chat page's own fetch on every turn.

## Browser checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | AC11 OpenEMR native portal login still lands on `portal/home.php` | PASS | `screenshot-1-native-portal-login-dashboard.png` |
| 1 | AC6 dashboard carries a visible AI Chat tile ("AI Chat", 197×184px) | PASS | same |
| 2 | AC2 first sign-in at top level lands on the dashboard | PASS | `screenshot-2-after-first-signin-dashboard.png` |
| 2 | AC2 it is **not** the standalone full-page chat | PASS | same |
| 2 | AC5 Chrome sent `Sec-Fetch-Dest: document`, answered 303 | PASS | CDP capture |
| 2 | exactly one AI session issued | PASS | ai-server SQLite row diff |
| 3 | AC5/AC7 opening the panel on a live session navigates nothing | PASS | `screenshot-3-chat-panel-open-on-dashboard.png` |
| 3 | AC7 in-panel launch answered **200** (chat served), not 302 (re-authorized) | PASS | CDP capture |
| 3 | AC5 Chrome sent `Sec-Fetch-Dest: iframe` | PASS | CDP capture |
| 4 | AC9 a turn from the portal origin is refused | PASS | HTTP 422, see note |
| 4 | AC9 the chat page's own same-origin fetch streams a reply | PASS | `screenshot-4-chat-turn-streams.png` |
| 5 | AC7 live session at top level → dashboard, never full-page chat | PASS | `screenshot-5-top-level-launch-dashboard.png` |
| 5 | AC5 `Sec-Fetch-Dest: document` on the top-level launch, answered 303 | PASS | CDP capture |
| 6 | AC2 re-authentication after AI-session expiry also lands on the dashboard | PASS | `screenshot-6-after-reauth-dashboard.png` |
| 6 | AC2 re-authentication is not answered with the standalone chat | PASS | same |
| 7 | AC4 a denial (`error=access_denied`) lands on the dashboard, no 422 | PASS | `screenshot-7-after-denial-dashboard.png` |
| 8 | AC3 `next=`/`redirect=` on the callback are discarded, destination unchanged | PASS | CDP capture |

Both sign-in paths (steps 2 and 6) went through the real OAuth2 **login form** and the
real **scope-consent** step (`scope-authorize.html.twig`) — the harness logs
`oauth step 1: login` / `oauth step 2: consent` for each.

## Notes on two results that are not what they first look like

**Step 4 returns 422, not 403.** FastAPI validates the `ChatTurnRequest` body *before*
the handler's Origin check runs. The browser check fires a CORS-*simple* request (no
`Content-Type`, so no preflight — the shape the Origin check actually defends against),
and its implicit `text/plain` is refused by the body model one step ahead of the Origin
check. Sending JSON instead triggers an `OPTIONS` preflight that dies at 405 and never
delivers the POST. Every route out of a cross-origin page is a rejection, which is all
that check claims. The Origin check's own **403** is asserted on a well-formed JSON body
in two other places: wrapper step 4 above, and
`test_chat.py::test_ac9_the_settings_split_did_not_disable_the_chat_origin_check`.
This ordering is pre-existing and untouched by this ticket.

**`Sec-Fetch-Dest` is absent from `Network.requestWillBeSent`.** The network stack adds
those headers after the renderer hands the request over, so they appear only on
`requestWillBeSentExtraInfo`. A harness reading the first event reports "header absent"
for a header that was in fact sent — which for this ticket would silently invert the
result. The capture reads the ExtraInfo event, and correlates redirect hops by
`requestId` (a 303 reuses the id, so the naive `findLast` attributes the launch hop's
headers to the dashboard hop). Both were harness defects found and fixed during this
run; neither was ever a product defect.

## Deviations

- The seeded patient's portal password is reset to a known value immediately before the
  run (`patient_access_onsite.portal_pwd`, pid 1) — same deviation and category as
  `evidence/TICK-054` and `evidence/TICK-055`.
- Step 6 reaches the expiry path by forcing `sessions.expires_at` into the past inside
  the ai-server container, rather than waiting out the 8-hour TTL. The re-authentication
  that follows is entirely real: full breakout, login form, consent screen.
- `deploy/local/.env` is gitignored and therefore not part of this worktree's commit. It
  was written locally for this run with the two split settings. **Any existing
  deployment must add `AI_SESSION_DASHBOARD_REDIRECT_URI` and `AI_SESSION_CHAT_ORIGIN`
  to its own `.env` and drop `AI_SESSION_SUCCESS_REDIRECT_URI`** — the compose entries
  carry `:?` guards, so `docker compose up` fails loudly until that is done. That is the
  intended behaviour of the rename (AC10), not a regression.
