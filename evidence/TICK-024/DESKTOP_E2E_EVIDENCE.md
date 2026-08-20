# TICK-024 — desktop Chrome E2E: partial pass, two real findings

**Executed:** 2026-08-20, real desktop Chrome via browser automation (not a
sandboxed worker) against the live local Docker topology.

## Result

Not complete. Login and portal launch verified live; the chat/onboarding/OCR/
appointment flows could not be reached because of a real, live OAuth
consent-form blocker (finding #2), on top of a real discoverability bug found
along the way (finding #1). Neither is a testing artifact — both reproduce
reliably and are documented below with enough detail to fix directly.

## What was verified live

1. **Portal login** (`AND-IFRAME-01`'s login step): logged in as synthetic
   patient `AverySubjecttest1` at `https://emr.localhost/portal/index.php?site=default`
   using the classic portal login form. Succeeded; landed on `portal/home.php`.
2. **AI Chat entry exists and the iframe genuinely renders**: confirmed via
   `document.getElementById('aeai-portal-chat')` — real layout box (`display:
   block`, `visibility: visible`, 1698x640px), not hidden or broken. `src`
   correctly points at `https://chat.localhost/oauth/launch` with no OpenEMR
   token/identifier in the DOM (FR-4 still holds).
3. **The OAuth/SMART launch chain executes correctly through login**: driving
   the exact same `oauth/launch` URL (in its own tab, to work around this
   browser-automation tool's cross-origin-iframe content restriction — the
   embedding itself was already separately proven in
   `evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md`) reached OpenEMR's own
   `/oauth2/default/provider/login` page, and a fresh login there (distinct
   from the portal session, matching the OAuth2 provider's own session model)
   succeeded and redirected to a consent screen.

## Finding 1 — AI Chat entry has no dashboard nav tile; only reachable by scrolling

`PortalChatController::render()` (`openemr_modules/aeai-portal-chat/src/
Controller/PortalChatController.php`) echoes a bare `<section id="aeai-portal-chat">`
via `RenderEvent::EVENT_SECTION_RENDER_POST`. This event fires once, appending
raw HTML after the portal's accordion-based dashboard content
(`templates/portal/home.html.twig`'s `#cardgroup` "Bootstrap accordion" — every
other portal feature has BOTH a dashboard grid tile linking to `#some-card`
AND a matching `.card.collapse` panel in the accordion). This section has
neither: no tile appears among the 10 dashboard buttons (Clinical Documents,
Appointments, Secure Messaging, Health Snapshot, Profile, Billing Summary,
Medical Reports, Settings, Help, Logout — confirmed by direct DOM inspection,
`AI Chat` is not among them), and the section itself isn't part of the
accordion (`collapse` class absent), so it's simply appended after everything
else on the page -- requiring the patient to scroll ~600px past a fully
rendered dashboard with no visual cue anything is below.

This means TICK-012's AC1 ("Only an authenticated patient can see and launch
the portal entry") is not really met at the UX level: the entry is present in
the DOM (satisfying the narrower `evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md`
proof, which never checked visual discoverability) but not something a real
patient would find without already knowing to scroll past the entire
dashboard. This is exactly the gap `tickets/TICK-012-build-portal-module.md`'s
own "Verification gap, on the record" note flagged as unverified at runtime --
now concretely confirmed.

**Not fixed here** (out of scope for a verification ticket to redesign a
render hook) -- recommend a follow-up ticket to add a proper dashboard tile,
likely via `RenderEvent::EVENT_DASHBOARD_INJECT_CARD` (the hook
`tickets/TICK-002-select-portal-hook.md`'s own four-row event table already
identified as the "documented intent" mechanism for exactly this
launcher-tile use case, but which TICK-002/012 didn't use).

## Finding 2 — the AI server's actual registered OAuth client uses staff (`user/*`) scopes, not patient (`patient/*`) scopes

Confirmed live from the running `local-ai-server-1` container's own
environment (`OPENEMR_OAUTH_CLIENT_ID=Ig917Lhc8KAHcc3P2CdNsfxqtJb_vpAJ-qr4KmBUGE4`)
and the matching `oauth_clients` row:

```
client_name: Intake Assistant (TICK-025 local)
client_role: user
scope: openid api:oemr user/patient.crus user/appointment.cruds
```

Every scope is `user/*` (staff-context), not `patient/*`. This is not new --
`evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` already recorded the identical
observation about the AI server's registered client and explicitly flagged it
as out of scope for TICK-002 ("flagging it here only because it surfaced while
confirming this callback, not as a TICK-002 finding"). This session confirms
it's still true and is now blocking real verification: because this client is
`user`-role, OpenEMR's OAuth2 provider does not treat it as eligible for
"Patient standalone apps Auto Approved" -- a genuine patient login here still
hits a full staff-style resource-permission consent screen (checkboxes for
`appointment`/`patient` create/read/update/delete/search actions), which a
real patient should never see for a scheduling chat assistant, and which this
session's browser automation could not cleanly complete (the consent form's
resource/action checkbox set is large and dynamically generated; replicating
every field via scripted POST proved unreliable within this session's time
budget).

**Not fixed here.** This is a real, pre-existing security/architecture gap
(the product's whole authorization-boundary premise is patient-context, not
staff-context -- see `ARCHITECTURE.md` and TICK-028's binding work) that
deserves its own investigation and fix, not a patch applied mid-way through an
E2E verification pass. Filed as a follow-up (see below).

## What remains unverified

Streaming chat, onboarding/OCR confirmation, appointment book/cancel through
the chat UI, dependency-failure fallback, and desktop accessibility checks
(`AND-COOKIE-01` through `AND-A11Y-01`-equivalent desktop cases) could not be
reached because finding 2 blocks obtaining a real `ai_session` cookie through
the actual patient-facing flow. TICK-013/017/031's own unit/integration test
suites (394 passing) cover this logic in isolation; this ticket's job was
specifically to prove the *integrated* flow works end to end through a real
browser, which finding 2 prevented.

## Recommendation

Two follow-up tickets before this ticket can be re-attempted to completion:
1. Fix the AI server's OAuth client registration to be patient-context
   (`patient/*` scopes, `client_role: patient` or equivalent), matching the
   architecture's actual security premise.
2. Add a real dashboard nav tile for the AI Chat entry.

Once both land, re-run this ticket's remaining cases against a client that
doesn't require a manual consent screen.
