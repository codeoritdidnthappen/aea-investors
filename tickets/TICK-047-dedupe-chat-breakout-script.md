---
id: TICK-047
title: "task(portal): deduplicate OAuth2 iframe-breakout script into shared base template"
type: task
epic: EPIC-04
priority: P3
estimate: XS
depends_on: [TICK-045]
labels: [portal, chat, cleanup]
source: [FR-2]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/94
builder_commit: 2bc12ee
---
## Context

Follow-up from an independent code review of TICK-045's fix. The
iframe-breakout `<script>` block TICK-045 added is duplicated verbatim in
both `openemr_overrides/templates/oauth2/oauth2-login.html.twig` (line 44)
and `openemr_overrides/templates/oauth2/scope-authorize.html.twig`. Both
templates extend `oauth2-base.html.twig`, which has an empty `scripts`
block that either could populate once instead.

The duplication was a deliberate, defensible tradeoff at the time (a
base-template override would widen the change's blast radius to every
OAuth2 page, not just the two TICK-045 verified), but it's a latent
maintenance risk: a future edit to the breakout logic (e.g. TICK-046's
fallback) requires remembering to update both files identically, and a
partial edit would let the two pages silently diverge in behavior.

## Acceptance Criteria

- [ ] The breakout script exists in exactly one place -- either moved into
      `oauth2-base.html.twig`'s `scripts` block (confirming this doesn't
      change behavior on any other OAuth2 page that extends it), or kept
      duplicated with an explicit code comment explaining why and pointing
      at the sibling file, if moving it is judged too risky.
- [ ] If moved to the base template: verify live that both
      `oauth2-login.html.twig` and `scope-authorize.html.twig` still
      breakout correctly, and that no other page extending
      `oauth2-base.html.twig` gains unwanted breakout behavior.

## Testing

If consolidated into the base template, live-verify the login and
scope-authorize flows both still breakout to a top-level page exactly as
before (real browser, not curl). CI must be green.

## Out of Scope

TICK-046's silent-failure fallback (may land before or after this ticket;
whichever lands second should carry the change forward to the other
file/location as needed).
