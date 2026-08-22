# TICK-055: how portal sign-out ends the AI session, and what it does not cover

TICK-055 AC2 requires that the chosen mechanism **and its failure modes** be recorded,
and states that "a logout that silently no-ops is a failure, not a degradation." This
is that record. Every fact below was measured against the running local topology on
2026-08-22, not inferred from documentation.

## The constraint that decides the design

The ticket suggests OIDC RP-initiated logout against the client's registered
`post_logout_redirect_uris`. Two measurements rule that out as the mechanism for *this*
direction, and a third rules out the obvious alternative.

**1. OpenEMR advertises `end_session_endpoint`, but not front- or back-channel logout.**

```
$ curl -sk https://emr.localhost/oauth2/default/.well-known/openid-configuration
end_session_endpoint:                   https://emr.localhost/oauth2/default/logout
revocation_endpoint:                    <ABSENT>
introspection_endpoint:                 https://emr.localhost/oauth2/default/introspect
frontchannel_logout_supported:          <ABSENT>
backchannel_logout_supported:           <ABSENT>
frontchannel_logout_session_supported:  <ABSENT>
```

`end_session_endpoint` is *RP-initiated*: it is the relying party sending the user to
the provider to end the provider's session. That is the chat → portal direction. What
TICK-055 needs is the portal → chat direction, which OIDC calls front-channel or
back-channel logout, and OpenEMR 8.3.0 advertises neither. There is no OIDC-native
mechanism here to use.

**2. The ticket's premise about the registration is inaccurate.** `PATIENT_AUTH.md:82`
is cited as showing `post_logout_redirect_uris` "already present in the registration."
That line is inside §3's `curl` registration of the **probe** client
(`scripts/probe_patient_context.py`), whose `redirect_uris` is
`http://localhost:8910/callback` and whose `post_logout_redirect_uris` is
`["http://localhost:8910/"]` — the probe's own loopback callback server. The AI
server's client is registered separately (`deploy/local/README.md:50-66`) and that
procedure never mentions `post_logout_redirect_uris`. Nothing was "already present"
for the client this ticket concerns.

**3. The portal session cookie is `SameSite=Strict`.** This is the decisive one.

```
$ curl -sk -i https://emr.localhost/portal/index.php | grep -i '^set-cookie'
set-cookie: App=PortalOpenEMR; ...; SameSite=strict
set-cookie: PortalOpenEMR=20223fdc...; ...; HttpOnly; SameSite=Strict
```

The natural design — rewrite the portal's sign-out link to `chat.localhost/logout`,
clear the AI session there, then redirect onward to `portal/logout.php` — **cannot
work**. That final hop is a cross-site top-level navigation, so Chrome withholds the
`SameSite=Strict` `PortalOpenEMR` cookie; `logout.php` requires `verify_session.php`,
finds no session, and the *portal* never actually logs out. The same applies to any
bounce through the chat origin. Sign-out must remain the same-site top-level
navigation it already is.

That leaves exactly one shape: the AI session is ended by a **cross-origin call the
portal page makes itself**, alongside the navigation rather than instead of it.

## The mechanism

`openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php` binds a
`click` handler to every portal sign-out anchor (`a[href$='logout.php']` — this matches
both entries the portal renders: the dashboard tile's `./logout.php` and the nav menu's
`logout.php`) which calls:

```js
navigator.sendBeacon("https://chat.localhost/api/logout")
```

`sendBeacon` is the only such call specified to survive the page unload that
immediately follows the click; a plain `fetch`/XHR is cancelled with the document. It
sends the `ai_session` cookie (`SameSite=None`) and an `Origin` header. It is also
neither `fetch(` nor `XMLHttpRequest`, both of which the module is separately forbidden
to contain (`ai_server/tests/test_portal_module.py:84`).

The AI server's `POST /api/logout` (`ai_server/app/main.py`) deletes the session row
outright and clears the cookie. It accepts the chat origin — exactly the discipline
`chat_turn` applies — plus, only when `AI_SESSION_PORTAL_ORIGIN` is configured, the
portal's own origin. That second entry is forced by the constraint above: every
portal-initiated call necessarily carries `Origin: https://emr.localhost`, so a
chat-origin-only check would reject it and make the whole hook the silent no-op AC2
calls a failure. Any *other* origin still cannot end the session, which is what AC4
requires; `POST /api/chat` continues to accept the chat origin only, so widening logout
did not widen the chat turn (`test_ac4_the_portal_origin_is_never_accepted_by_a_chat_turn`).

## Failure modes — what this does NOT cover

1. **Sign-out from a portal page other than the dashboard is not intercepted.** Both
   `RenderEvent::EVENT_DASHBOARD_INJECT_CARD` and `EVENT_SECTION_RENDER_POST` fire only
   from `portal/home.php` (`evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md:40-52`), so the
   hook script is only present on the dashboard. A patient who signs out from another
   portal page ends their portal session and leaves the AI session running to its TTL.
   In the current portal this is a narrow gap — the dashboard is where both sign-out
   controls live — but it is a real one, and closing it needs a hook that fires on
   every portal page, which this module does not have.

2. **The portal's own inactivity auto-logout is not intercepted.** `home.php`'s
   `logout()` (templates/portal/home.html.twig:163-165) does
   `location.replace("./index.php?...")` on a timer; it is not a click on a
   `logout.php` anchor, so the beacon does not fire.

3. **The beacon is fire-and-forget.** `navigator.sendBeacon` returns only whether the
   request was *queued*; the page is gone before any response arrives, so the browser
   cannot confirm the server accepted it. A browser without `sendBeacon`, or a network
   failure at that instant, leaves the AI session alive until its TTL. The server-side
   witness in `run_live_verification.sh` (a diff of the ai-server's own request log,
   confirming exactly one additional `POST /api/logout` answered `204`) exists
   precisely because the browser cannot self-report this.

4. **Third-party cookie blocking.** The `Set-Cookie` that clears `ai_session` arrives
   on a cross-site response. If the browser blocks it, the cookie survives in the
   browser — but the *row is already deleted server-side*, so the stale handle is dead
   and `/api/chat` 401s. That is the failure direction that matters, and it is the one
   the live run measured.

5. **The delegated OpenEMR token is not revoked.** See below.

## Finding: OpenEMR 8.3.0 exposes no token revocation endpoint

TICK-055's Out of Scope says: "Revoking the delegated token at OpenEMR's end, if the
release exposes no revocation endpoint — if it does not, that is recorded as a finding
rather than worked around."

It does not. The discovery document above has **no `revocation_endpoint`** (it has
`introspection_endpoint`, which reports on a token but cannot invalidate one). So after
logout, the AI server has destroyed its copy of the patient's encrypted access and
refresh tokens, but the tokens themselves remain valid at OpenEMR until they expire on
their own. Nothing in this repo can shorten that.

This is recorded, not worked around. It bounds what logout can promise: it ends the AI
session and destroys this system's copy of the credentials; it does not and cannot
revoke the grant at the identity provider.
