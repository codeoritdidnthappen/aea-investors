---
id: TICK-054
title: "bug(portal): dashboard render starts an OAuth flow and throws the patient off the page"
type: task
epic: EPIC-04
priority: P1
estimate: M
depends_on: [TICK-032, TICK-045]
labels: [portal, chat, auth, bug]
source: [FR-2, FR-37]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/108
builder_commit: null
---
## Context

User report (2026-08-22), confirmed by the reporter against a running stack:
after signing in to the patient portal the patient is taken to the chat
without ever clicking AI Chat.

`PortalChatController::render()`
(`openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php:70-78`)
emits the panel with a live `src`:

```
<iframe title="AI Chat" data-aeai-portal-chat="1"
        src="https://chat.localhost/oauth/launch" ...></iframe>
```

There is no `loading="lazy"`, no `data-src`, and no click handler. TICK-032
put that panel inside a Bootstrap `.collapse` card, so it is `display:none`
until the tile is clicked -- but a hidden iframe still loads. The launch
therefore fires when the dashboard paints, for a panel the patient has not
opened. What follows:

1. `/oauth/launch` 302s to OpenEMR's authorize endpoint.
2. The OAuth2 provider keeps its own session, **separate from the portal
   session** -- documented in
   `evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md:29-30` ("a fresh login there
   (distinct from the portal session, matching the OAuth2 provider's own
   session model)"). So it renders `/oauth2/default/provider/login`.
3. That login page renders inside the hidden panel, so TICK-045's breakout
   script sees `window.top !== window.self` and navigates the **top-level**
   window to it.

The patient signs in to the portal, the dashboard paints, and they are
immediately thrown off it onto a second sign-in page having clicked nothing.
The breakout is behaving exactly as TICK-045 intended; the defect is that
an unopened panel started the flow at all.

This is the first half of the reported bug. Where the patient lands *after*
that second sign-in is TICK-051, which depends on this ticket: the dashboard
cannot be a safe redirect destination while rendering it re-triggers a
launch.

## Acceptance Criteria

- [ ] Rendering the portal dashboard issues no request to
      `/oauth/launch` and starts no OAuth flow. Verified by network capture
      on a dashboard load, not by reading markup.
- [ ] The panel's authorization begins only when the patient opens the AI
      Chat tile, and opening it still works with both mouse and keyboard
      (NFR-19's existing bar for this panel).
- [ ] No OpenEMR token or patient identifier enters the DOM as part of the
      deferred load. FR-4 and TICK-002's single-AI-server-origin property
      still hold: the panel's only network target remains the AI server.
- [ ] The tile, accordion behaviour, and `data-parent="#cardgroup"`
      grouping TICK-032 established are unchanged.

## Testing

Live verification against the local Docker topology with real desktop
Chrome, matching TICK-024's and TICK-045's bar: sign in as a seeded
synthetic patient, capture the network on dashboard load and confirm no
`chat.localhost` request, then open the tile and confirm the chat loads.
Repeat with the AI session expired and confirm the patient is not moved
until they open the panel. Record under `evidence/TICK-054/`.

Note the hazard recorded for this repo: the OpenEMR module is bind-mounted
per-file, so mounts can go stale or truncate. Diff the in-container
controller against the host copy before trusting the result.

## Out of Scope

Where an authorization lands the patient (TICK-051). Avoiding a redundant
authorization when the panel is reopened with a live session: that is
`/oauth/launch`'s short-circuit, delivered by TICK-051 AC5, and asserting it
here would make this ticket unbuildable in dependency order. The breakout script
(TICK-045) and its fallback banner (TICK-046), both of which are correct
and stay. The separate question of whether OpenEMR's OAuth2 provider can be
made to accept an existing portal session instead of demanding a second
sign-in -- that is upstream behaviour and needs its own spike.
