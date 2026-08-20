# TICK-037 — OAuth consent-screen scope-drop bug, live evidence

Executed against the running local Docker topology (`local-openemr-1`,
`local-mariadb-1`, `local-caddy-1`), the same shared stack `evidence/TICK-002`,
`evidence/TICK-024`, `evidence/TICK-028`, and `evidence/TICK-033` used.

## Result

Before the fix, a real patient login through `/oauth/launch` → consent →
onboarding-start always failed with a `401` from OpenEMR:

```
OpenEMR.ERROR: scope patient/assessment.c not in access token
{"exception":"[object] (OpenEMR\\Common\\Acl\\AccessDeniedException(code: 0):
scope patient/assessment.c not in access token at
/var/www/localhost/htdocs/openemr/src/RestControllers/Subscriber/AuthorizationListener.php:192)
...
"path":"/apis/dispatch.php/default/portal/patient/assessment"}
```

After the fix (this ticket's `openemr_overrides/templates/oauth2/scope-authorize.html.twig`),
the identical live flow — same patient, same client, same chat turn — produces
a real `201`:

```sql
SELECT uuid, patient_uuid, status, created_at FROM aeai_assessment_draft
ORDER BY created_at DESC LIMIT 1;

uuid                                   patient_uuid                            status   created_at
768695b5-3ebd-4ba6-a8df-bee50c8b5006   a28b0cf9-f4c8-4674-81fc-ec99365c12bb   draft    2026-08-20 20:26:10
```

Confirmed a second time after a full `docker compose up -d openemr` recreation
(bind mount, not a live-container hand-edit) — the fix survives a redeploy and
the login → 201 → DB row result reproduced identically.

## Root cause: two independent bugs in OpenEMR's own vendor consent template

`templates/oauth2/scope-authorize.html.twig` is not a project file — it is
OpenEMR 8.3.0's real, unmodified vendor template (from real upstream commits
merging `openemr/openemr#9457` and `#9466`, "granular scopes", closing issue
`#8639`; confirmed against the actual `v8_3_0` tag on GitHub, including a
verified, GPG-signed commit history, before treating it as genuine).

Diagnosed with temporary instrumentation in a throwaway copy of
`ScopeRepository.php` inside the running container (reverted before this
ticket's real fix was written — never shipped), plus a client-side
`form.submit()` interceptor run directly in the browser console during a real
consent-screen click, which captured the actual scope strings about to be
POSTed:

```
Before fix: assessment never appears in the POST body at all.
Interim (partial) fix: [{"name":"scope[patient/assessment.cru]","value":"patient/assessment.cru"}, ...]
Final fix:  [{"name":"scope[patient/assessment.c]", ...}, {"name":"scope[patient/assessment.r]", ...}, {"name":"scope[patient/assessment.u]", ...}, ...]
```

1. **Drop bug**: `reconstructV2Scope()`'s JS only emits a scope input via its
   restricted-category loop or its `unrestricted`-flagged branch. A resource
   with no restriction sub-categories and `isUnrestricted=false` (the
   module's server-side scope-structuring code defaults unrecognized custom
   resources to `false`) falls into neither branch — its checkbox displays
   checked, but nothing is ever added to the submitted form.
2. **Join bug**: once made to emit something, the original code joined every
   checked action into one combined string per resource (e.g.
   `patient/assessment.cru`). Read directly from the pinned image's own
   source:
   - `ScopeEntity::containsScope()` (`src/Common/Auth/OpenIDConnect/Entities/ScopeEntity.php`)
     checks that `$this`'s own individual permission flags are a superset of
     the target's.
   - `ResourceScopeEntityList::containsScope()` (`.../Entities/ResourceScopeEntityList.php`)
     iterates a resource's *individually registered* scope entities and
     checks each one **on its own** — it never unions permissions across
     entries in the list.
   - The module registers `patient/assessment.c`, `.r`, `.u` as three
     separate single-action `addScope()` calls
     (`AssessmentDraftController::addScopes()`), so no single registered
     entity alone has `create && read && update` all `true` — a combined
     `.cru` request satisfies none of them and is silently dropped in
     `AuthorizationController::updateAuthRequestWithUserApprovedScopes()`,
     never reaching `finalizeScopes()` or the issued token.

Fix: emit one atomic `${context}/${resource}.${action}` scope string per
checked action (never joined), and resources with no restriction options
honor the master checkbox directly. Verified strictly backward-compatible:
an atomic single-action scope is always contained by a resource whose scopes
happen to be registered pre-combined (as this app's core FHIR resources —
`patient/Patient.read`, `patient/Appointment.read` — already are), so nothing
that worked before regresses; confirmed live (those two scopes still round-trip
correctly in the same test runs above).

## What changed

New file `openemr_overrides/templates/oauth2/scope-authorize.html.twig`
(OpenEMR's real vendor file, patched), bind-mounted read-only over the vendor
path in `deploy/local/docker-compose.yml` — same pattern as the existing
`openemr_modules/aeai-portal-chat` mount (TICK-012). No file inside the
checked-out `openemr/openemr:8.3.0` image itself was edited in place.

## Cancellation (`patient/appointment.u`, TICK-036) — reasoned, not yet live-proven

`patient/appointment.u` is always requested/approved as a single checked
action, so the join bug (bug 2 above) never applied to it — a single action
never gets joined with anything. It may have been independently affected by
the drop bug (bug 1) depending on its resource's server-side `isUnrestricted`
flag; not yet isolated which flag it carries, and not yet exercised through a
real cancellation attempt in this pass. Tracked for live verification under
TICK-036 itself, not blocking this fix.
