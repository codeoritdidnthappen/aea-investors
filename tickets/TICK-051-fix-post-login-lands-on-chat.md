---
id: TICK-051
title: "bug(chat): after signing in the patient lands on the full-page chat instead of the dashboard"
type: task
epic: EPIC-04
priority: P1
estimate: M
depends_on: [TICK-045, TICK-046, TICK-054]
labels: [chat, auth, bug]
source: [FR-2, FR-31]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/104
builder_commit: 8b5cb17
---
## Context

User report (2026-08-22): after entering credentials the patient is
redirected to the chat. They must land on the portal dashboard.

TICK-054 covers the first half -- the dashboard starting an OAuth flow for a
panel nobody opened. This ticket covers where the patient ends up once a
sign-in has actually happened.

`GET /oauth/callback` (`ai_server/app/main.py:253-258`) answers
`303 -> configured_settings.success_redirect_uri`, and
`AI_SESSION_SUCCESS_REDIRECT_URI` is `https://chat.localhost/`
(`deploy/local/.env`, `deploy/local/.env.example`). Because TICK-045's
breakout has already moved the patient to top level to sign in, that 303
lands them on the standalone chat page full-screen, with the portal gone.

`GET /oauth/launch` (`main.py:245-250`) compounds it: it starts a fresh
authorization unconditionally and never checks for an existing `ai_session`,
so a patient who already has a valid session re-runs the whole OAuth dance
every time the panel opens.

### The trap: one setting doing two jobs

| Site | Job |
|---|---|
| `ai_server/app/main.py:258` | post-sign-in redirect target |
| `ai_server/app/main.py:202` | the `Origin` allowlist for `POST /api/chat` |

The second is the *only* CSRF defense on that route: the session cookie is
deliberately `SameSite=None` so it survives the cross-site iframe, and
`main.py:260-268`'s own comment says the Origin check is therefore the
actual defense. Repointing `AI_SESSION_SUCCESS_REDIRECT_URI` at the
dashboard would fix the redirect **and** make `emr.localhost` the only
origin allowed to call the chat API while 403-ing the chat page's own
`fetch()`. Chat breaks on every turn. Split the two settings before
touching the destination.

### Resolving the destination without a return URL

The callback is one shared endpoint and cannot be told apart by URL. It does
not need to be. The rule is stated over the flow's **position**, not the
patient's intent -- "did they type a password?" is unknowable here, since a
live OAuth2 provider session completes the exchange with no prompt whether
the flow runs at top level or in the panel.

Position is knowable server-side: browsers send `Sec-Fetch-Dest: document`
for a top-level navigation and `Sec-Fetch-Dest: iframe` for one into a
frame. So `/oauth/callback` stays a `303` and needs no client-side
interstitial. Absent or unrecognised values are treated as top level,
because the dashboard strands nobody. FR-31 and ADR-8 forbid any
`next=`/`redirect=` parameter, and none is required.

## Acceptance Criteria

- [ ] The redirect target and the `POST /api/chat` origin allowlist are two
      separate settings. Neither can be changed by editing the other, and
      the boot-time absolute-URL validation at `ai_server/app/auth.py:109-115`
      applies to both.
- [ ] An authorization completing at **top level** ends on the portal
      dashboard. This holds for first sign-in, for re-authentication after
      the AI session expires (TICK-045's breakout path), after the OAuth
      consent step (`scope-authorize.html.twig`), and for a direct top-level
      visit to `/oauth/launch` that completes with no prompt at all.
- [ ] The destination is unconditional: it does not vary with the portal
      page the patient was on when the session ended. `/oauth/callback`
      honours `code` and `state` only: any other query parameter is
      explicitly discarded rather than acted on, by an allowlist in the
      handler and not merely by FastAPI's signature binding, so a later
      return-URL parameter cannot be quietly honoured. Discarded, **not**
      rejected -- rejecting unknown parameters would break the authorization
      denial path, which arrives as
      `?error=access_denied&error_description=...&state=...` from
      `scope-authorize.html.twig`, and RFC 9207's `iss`.
- [ ] A denial (`error=access_denied`) returns the patient to the portal
      dashboard with no session issued, rather than 422-ing or stranding
      them. It is an expected outcome, not a malformed request.
- [ ] An authorization completing **inside the panel** loads the chat in
      that panel and navigates nothing. `Sec-Fetch-Dest` is what
      distinguishes the two; an absent or unrecognised value is treated as
      top level.
- [ ] Landing on the dashboard after a top-level authorization leaves the
      patient one deliberate click from the chat. Bootstrap `.collapse` does
      not persist across a page load, so the panel will be closed -- that is
      accepted, and the tile must be present and obvious (TICK-032) rather
      than the patient being left to hunt for it.
- [ ] `GET /oauth/launch` skips the authorization round trip when a valid
      `ai_session` is already present -- serving the chat when it is running
      in the panel, and redirecting to the dashboard when it is running at
      top level. The short-circuit obeys the same `Sec-Fetch-Dest` rule as
      the callback; it is not an exception to it. A live session reached at
      top level must never be answered with the full-page chat.
- [ ] `POST /api/chat` still accepts the chat page's own same-origin fetch
      and still rejects a request carrying any other `Origin`, or none. A
      test asserts the rejection, so the split cannot silently disable the
      CSRF check.
- [ ] `deploy/local/.env`, `deploy/local/.env.example`, and
      `deploy/local/docker-compose.yml:130` are updated together. That
      compose entry passes the value through with a required-variable guard
      (`${AI_SESSION_SUCCESS_REDIRECT_URI:?...}`), so a new setting without
      a matching compose entry either fails the boot check or silently
      leaves the container on the old single value. Each carries a comment
      stating the two settings are not interchangeable.
- [ ] The redirect setting is **renamed**, not reused.
      `ai_server/app/auth.py:100-104` fails only on *missing* variables, so
      keeping `AI_SESSION_SUCCESS_REDIRECT_URI` while silently re-meaning it
      lets a deployment that adds the new origin variable and forgets to
      repoint the old one boot cleanly with the bug still present and no
      error anywhere. A rename makes that boot check fire.
- [ ] OpenEMR's native portal login is confirmed to still land on
      `portal/home.php`. It is not modified by this ticket, but FR-31 covers
      it, so it is verified rather than assumed.

## Testing

Unit tests over `/oauth/callback` driving `Sec-Fetch-Dest` directly
(`document` -> dashboard, `iframe` -> chat, absent -> client-side frame
check), asserting an unexpected query parameter is discarded and that an
`error=access_denied` callback returns to the dashboard without a session,
over `/oauth/launch`'s short-circuit with a live session, and over
`chat_turn`'s origin check with the settings split -- good origin accepted,
foreign origin and absent origin both 403.

Then live verification against the local Docker topology with real desktop
Chrome, matching the bar TICK-045 and TICK-024 set: sign in as a real seeded
patient, open the chat, let the AI session expire, re-authenticate, and
confirm the dashboard is where the patient lands every time credentials are
entered, and that a chat turn still streams a reply afterwards. Record under
`evidence/TICK-051/`. CI must be green.

Note the hazard recorded for this repo: `ai-server` is not bind-mounted, so
rebuild with `--build` before verifying, and diff the in-container twig
against the host copy before trusting a template change.

## Out of Scope

Deferring the panel's launch until the patient opens it (TICK-054, which
this depends on). Changing the TICK-045 breakout script or the TICK-046
fallback banner -- both correct, both stay. The absence of any logout or
session-revocation path in the AI server, which is a separate defect. The
chat UI itself and the TICK-032 dashboard tile.

**Do not** "simplify" the two settings back into one, and do not reintroduce
a return-URL parameter. See ADR-8.
