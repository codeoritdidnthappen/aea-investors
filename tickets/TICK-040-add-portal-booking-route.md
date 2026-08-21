---
id: TICK-040
title: "bug(scheduling): booking's write route is structurally unreachable for a genuine patient token"
type: task
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-017, TICK-034, TICK-036, TICK-037]
labels: [scheduling, openemr, auth]
source: [FR-11, FR-12, FR-13, FR-20, FR-28]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/83
blocked_reason: "live-proof acceptance criteria (AC2, AC4) require registering the new scope and exercising it through a real OAuth consent + booking write, but the only reachable local OpenEMR Docker instance mounts the main checkout, not this isolated worktree \u2014 making the new module code unreachable without writi"
---
## Context

Found live 2026-08-20 while independently verifying TICK-020's reschedule
evidence, which flagged (but did not fully root-cause) that "nothing in this
codebase resolves whether a genuine patient OAuth token actually carries a
scope OpenEMR's booking route accepts." Root-caused directly from the pinned
image's own source, not a hypothesis:

`BookingService`/`OpenEmrBookingAdapter` (TICK-034) call the Standard API
route `POST /api/patient/:pid/appointment`
(`apis/routes/_rest_routes_standard.inc.php:403`). That route's handler
calls `RestConfig::request_authorization_check($request, "patients", "appt")`
(`AppointmentRestController::post()`), which resolves to
`AclMain::aclCheckCore($section, $value, $request->getSession()->get("authUser"), ...)`
(`src/RestControllers/Config/RestConfig.php:180-193`) -- a **staff ACL**
check against a logged-in OpenEMR staff username, not an OAuth scope check
at all. `_rest_routes.inc.php`'s own comment confirms this by design:
"Note that the api route is only for users role (there is a mechanism in
place to ensure only user role can access the api route)."

A genuine patient-context OAuth session (the only kind TICK-033 allows this
client to hold, and the only kind ARCHITECTURE.md's security premise
permits) has no staff ACL identity at all -- `aclCheckCore()` cannot ever
succeed for one. This is not a missing-scope configuration issue (unlike
TICK-037/TICK-039, both fixed today); it is structurally impossible for the
current route to ever accept a real patient token, regardless of what scope
is requested or granted.

The Portal API (`apis/routes/_rest_routes_portal.inc.php`, the route family
patient tokens actually work against, enforced via `AuthorizationListener`'s
OAuth-scope check -- the mechanism TICK-036's and TICK-017's own module
routes already use successfully) has **no appointment-create route at all**,
only `GET /portal/patient/appointment` and `GET /portal/patient/appointment/:auuid`.

This means booking has been unreachable for a genuine patient this entire
project, independent of and in addition to ADR-3's separate, already-known
"no candidate-slot source" gap (`NoMappedCandidateSource`) -- fixing ADR-3's
gap alone would still not make booking work, because the write call itself
has nowhere valid to land.

## Acceptance Criteria

- [ ] A new module-added portal route (`RestApiCreateEvent`, the same
      mechanism `AssessmentDraftController` (TICK-017) and
      `AppointmentCancelController` (TICK-036) already use, registered
      under `openemr_modules/aeai-portal-chat`) exposes appointment
      creation, enforced by `AuthorizationListener`'s OAuth-scope check, not
      staff ACL.
- [ ] The new route's scope (e.g. `patient/appointment.c`) is added to the
      AI server's requested/registered client scopes (`AuthSettings.scopes`,
      `ai_server/app/auth.py`) and consented on the OAuth screen -- proven
      live through the real consent flow (the same live proof pattern
      `evidence/TICK-037` used), not assumed to just work because a scope
      string was added.
- [ ] `BookingService`/`OpenEmrBookingAdapter` (TICK-034) is repointed from
      `POST /api/patient/:pid/appointment` to the new portal route; the
      Standard API route is not used for a patient-context booking write
      anywhere in this codebase after this ticket.
- [ ] A genuine patient login, with a real available slot token, results in
      a real OpenEMR-side appointment write -- proven live (blocked on
      ADR-3's separate no-candidate-slot gap until an availability source
      exists; until then, provable with a slot token seeded the same way
      this ticket's own investigation seeded a test appointment, not through
      the live candidate-discovery path).
- [ ] TICK-020's reschedule composition and TICK-034's booking both inherit
      this fix automatically (no separate change needed in either) once
      `OpenEmrBookingAdapter` is repointed.

## Testing

Live verification against the local Docker topology: register/consent the
new scope, seed a real target slot the same way this ticket's investigation
seeded a test appointment (bypassing the separate ADR-3 gap for this proof
only), and confirm a real patient-token booking write succeeds where the
Standard API route provably cannot. CI must be green.

## Out of Scope

Building a real availability/candidate-slot endpoint (ADR-3's separate,
already-known gap -- would need its own ticket). Staff-facing booking flows
(unaffected; staff already use the Standard API route through their own ACL
identity, which this ticket does not touch).
