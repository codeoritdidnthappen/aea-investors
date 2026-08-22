---
id: TICK-046
title: "task(portal): add fallback when chat iframe-breakout navigation is silently blocked"
type: task
epic: EPIC-04
priority: P2
estimate: S
depends_on: [TICK-045]
labels: [portal, chat, auth, bug]
source: [FR-2, NFR-19]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/93
builder_commit: 9cbdb74
---
## Context

Follow-up from an independent code review of TICK-045's fix
(`openemr_overrides/templates/oauth2/oauth2-login.html.twig:46`). TICK-045
fixed the AI Chat panel getting stuck on an embedded, unusable
login/consent flow by breaking the iframe out to a full top-level page via
`window.top.location.href = ...`.

That breakout call has no fallback if the browser silently blocks or
no-ops it. This isn't theoretical: TICK-045's own live-verification pass
(`evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`) already reproduced
this exact failure mode once, when the login page and the top-level frame
were briefly cross-origin (`chat.localhost` vs `emr.localhost`) during
testing -- Chrome's framebusting protection silently prevented the
top-navigation with no error and no user-visible signal. Local dev and OCI
prod currently route both hostnames to the same OpenEMR app so this
shouldn't happen in production today, but nothing in the code guards
against topology drift (a WAF/CDN added later, a proxy that terminates on
a different hostname, etc.). If it ever fires again, the patient is
silently left exactly where the original TICK-045 bug trapped them, with
zero indication anything is wrong.

## Acceptance Criteria

- [ ] If `window.top.location.href = ...` does not result in a top-level
      navigation within a short timeout, the user sees a visible,
      actionable message (e.g. "Click here to sign in") instead of a
      silently stuck panel.
- [ ] The fallback is verified to actually fire when the breakout is
      blocked (reproduce the cross-origin case from
      `evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`, or an
      equivalent deterministic block), not just reasoned about.
- [ ] The normal (same-origin) breakout path continues to work exactly as
      it does today -- verify live in a real browser, not just via curl.

## Testing

Reproduce the blocked-breakout case (cross-origin parent/child framing, as
TICK-045's live verification hit) and confirm the fallback message
appears. Reproduce the normal same-origin case and confirm the existing
top-navigation still happens with no regression. CI must be green.

## Out of Scope

Redesigning the breakout mechanism itself (e.g. switching away from
`window.top.location.href`) or addressing TICK-047's script duplication.
