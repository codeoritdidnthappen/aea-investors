---
id: TICK-032
title: "bug(portal): add a dashboard nav tile for the AI Chat entry"
type: task
epic: EPIC-04
priority: P1
estimate: S
depends_on: [TICK-012]
labels: [portal, frontend, accessibility]
source: [FR-1, FR-2]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/63
builder_commit: 1907330
---
## Context

Found live during TICK-024's real desktop Chrome E2E pass
(`evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md`, finding 1). `PortalChatController::
render()` echoes a bare `<section id="aeai-portal-chat">` via `RenderEvent::
EVENT_SECTION_RENDER_POST`. Every other portal feature has both a dashboard
grid tile and a matching accordion panel; this section has neither -- no tile
among the 10 dashboard buttons, and the section isn't part of the accordion
(no `collapse` class), so it's just appended after everything else on the
page, requiring ~600px of scroll past a fully-rendered dashboard with no
visual cue anything is below.

`tickets/TICK-002-select-portal-hook.md`'s own four-row event table already
identified `RenderEvent::EVENT_DASHBOARD_INJECT_CARD` as the documented-intent
mechanism for exactly this launcher-tile use case; TICK-002/012 used
`EVENT_SECTION_RENDER_POST`
instead, which doesn't create a tile.

## Acceptance Criteria

- [ ] The AI Chat entry appears as a dashboard tile alongside the other 10
      (Clinical Documents, Appointments, etc.), visible without scrolling.
- [ ] Clicking the tile reveals/launches the chat iframe using the existing
      accordion pattern (`data-parent="#cardgroup"`), consistent with how
      every other portal feature behaves.
- [ ] The iframe's existing properties (no OpenEMR token/identifier in the
      DOM, single AI-server-origin request) are unchanged.

## Testing

Live verification against the local Docker topology with real desktop Chrome
(matching TICK-024's own testing bar) -- confirm the tile is visible and
reachable without scrolling, and that clicking it launches the chat exactly
as the previously-hidden section did. CI must be green.

## Out of Scope

Redesigning the chat UI itself; TICK-033 (OAuth client scope fix).
