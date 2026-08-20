# TICK-017 — assessment-draft endpoint evidence

## Result

OpenEMR v8.3.0 has no native write endpoint for a patient-writable assessment draft
(`evidence/TICK-001/ENDPOINT_MATRIX.md`, "Start/checkpoint assessment draft" /
"Complete structured assessment" rows — both confirmed absent against the pinned
tagged source and, separately, against OpenEMR's unreleased `master` branch). This
adds one, entirely through OpenEMR's own module/event extension system
(`OpenEMR\Events\RestApiExtend\*`) — no core file modified, the pinned
`openemr/openemr:8.3.0` image untouched. Proven against the live local stack with a
real `authorization_code`+PKCE patient token (never a staff/admin credential), the
same discipline `evidence/TICK-028/BINDING_MATRIX.md` used.

## Why this route, and why it's safe

OpenEMR's core `AuthorizationListener` (`src/RestControllers/Subscriber/
AuthorizationListener.php`) unconditionally rejects any FHIR write from a
patient-role token, independent of which route or module registers it — a
deliberate, acknowledged-temporary policy (`"TODO: ... look at opening up patient
write access to data"`), not a bug. Registering a new FHIR `QuestionnaireResponse`
write route would be reachable but always 403. The Portal API namespace
(`/portal/...`, `apis/routes/_rest_routes_portal.inc.php`) carries no such block —
its 5 built-in routes are already patient-role reads; this adds two more that are
patient-role writes, through the same `RestApiCreateEvent`/`addToPortalRouteMap`
mechanism.

Binding: every handler scopes to `HttpRestRequest::getPatientUUIDString()`, set only
by `BearerTokenAuthorizationStrategy` from the validated bearer token
(`src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:227,444`) —
never client input. A cross-patient request 404s because the row is never found by
that scoped query, not because it was found and then rejected — there is no code path
in `AssessmentDraftService` that can return another patient's row.

New code: `openemr_modules/aeai-portal-chat/src/Controller/AssessmentDraftController.php`
(route + scope registration), `.../src/Service/AssessmentDraftService.php`
(validation + persistence, scoped `ONBOARDING_CONTRACT.md` fields 6–9), and
`.../sql/table.sql` (module-owned table, no core schema touched).

## Environment provisioning (2026-08-20)

- `aeai-portal-chat` module registered live (`modules` row, `type=0`, `mod_active=1`)
  — it had never actually been installed despite TICK-012 shipping its source;
  recorded as a gap in TICK-012's own ticket file at the time.
- `AI_SESSION_SUCCESS_REDIRECT_URI` in the local `.env` was stale
  (`https://emr.localhost/portal/home.php`, pre-dating the `chat.localhost` value
  `.env.example` documents) — fixed to match, `ai-server` image rebuilt and
  restarted, since it hadn't picked up any code merged since 2026-08-19T18:58.
- The registered AI-server OAuth client's `scope` column was extended with
  `api:port patient/assessment.c patient/assessment.r patient/assessment.u` (a
  client's requested scopes are filtered against its own registered list at grant
  time, regardless of what the server considers "supported"), and the first full
  probe run passed against it. Mid-session, something outside this work recreated
  the `local-openemr-1` container a second time (confirmed via `docker ps` showing a
  fresh `CREATED AT` with no corresponding action taken here), which invalidated
  that client's stored secret (`invalid_client` on token exchange afterward, cause
  unconfirmed — possibly the site's own secret-encryption material tied to that
  container instance). Rather than debug a container state this work didn't change,
  the final, reproducible run used a second OpenEMR OAuth client registered fresh
  via `POST /oauth2/default/registration` (Dynamic Client Registration, the same
  mechanism `deploy/local/README.md` step 3 documents) requesting the same scopes —
  same result, all 9 checks passing (below).
- Synthetic patients pid 1 (Avery Subjecttest) and pid 2 (Jordan Controlcase, used as
  the cross-patient control) had their portal passwords reset via direct SQL
  (`password_hash(..., PASSWORD_DEFAULT)`, matching `AuthHash`), pid 2 previously
  having no portal-access row at all. Same category of deviation already on record
  for TICK-002/TICK-028: environment provisioning for a synthetic fixture, not a
  product data path. Plaintext values were never written to a file or logged, and are
  not retained anywhere in this repository.

## Live proof (`scripts/probe_assessment_draft.py`)

Non-interactive: drives OpenEMR's own OAuth2 login form (`POST
/oauth2/default/login`, discovered to be a distinct flow from the classic portal
login) and its consent screen (approving every offered scope, the same as a patient
clicking "Authorize" with every box checked, its default state) via plain HTTP form
submission — no JavaScript execution involved, so no browser-automation tool was
needed. Two real patient-context tokens obtained this way, then exercised end to end
against the live route:

| # | Step | Result |
|---|---|---|
| 1 | Patient A submits a syntactically invalid JSON body | `400` |
| 2 | Patient A creates a draft (`POST /portal/patient/assessment`, `help_type` only) | `201` |
| 3 | Patient A reads it back | `200`, `help_type` present |
| 4 | Patient A checkpoints `preferred_contact_method`/`contact_value` | `200`, both fields now present |
| 5 | Patient A switches `preferred_contact_method` alone, no `contact_value` | `400`, honest "required" message |
| 6 | Patient A selects `other_accommodation` with no detail supplied | `200` (optional per contract) |
| 7 | Patient A submits a non-array `accommodations` value | `400` |
| 8 | **Patient B reads patient A's draft by uuid** | `404` |
| 9 | **Patient B writes patient A's draft by uuid** | `404` |
| 10 | Patient A submits an invalid `help_type` enum value | `400` |
| 11 | Patient A requests completion with 2 of 4 required fields present | `400` |
| 12 | Patient A supplies the remaining required fields and completes | `200`, `status=completed` |
| 13 | Patient A attempts to edit the now-completed draft | `409` |

All 13 checks passed (`scripts/probe_assessment_draft.py` exit 0). Steps 8–9 are the
binding proof: same mechanism TICK-028 found *missing* on the FHIR `Patient` write
route, here confirmed present on this new route because it reuses OpenEMR's own
token-derived patient-UUID rather than trusting any client-supplied identifier.

### Fixes from the merge-gate code review

A `/code-review` pass against this branch found and fixed four issues before merge:

- Malformed JSON was silently treated as an empty body (`(array) null === []`),
  returning `201`/silent-no-op instead of `400` — step 1 above locks this in.
  `AssessmentDraftController` now parses the body once (`parseJsonBody()`) and
  returns `400` on a decode failure.
- `update()`'s read-then-write had a TOCTOU window: two concurrent requests for the
  same draft could both pass the "not yet completed" check before either write
  landed. The final `UPDATE` now adds `AND status != 'completed'` (compare-and-swap)
  and checks `QueryUtils::affectedRows()`, returning the same `409` a losing request
  would have gotten from the read-time check.
- The `POST`/`PUT` closures had duplicated body-parsing logic; extracted into one
  `parseJsonBody()` method.
- The Python static test for "no SQL interpolation" only inspected the source line
  where a `sql*()` call opened, missing the continuation lines where the actual SQL
  string and bound-parameter array live (every real call in this file is
  multi-line) — it would have passed even if a future edit interpolated
  `$patientUuid` directly into a query string. Rewritten to isolate and check the
  quoted SQL string specifically (the bound-parameter array legitimately references
  those variables; only the string itself must not).

A second review round on the same branch found one more: the `status != 'completed'`
compare-and-swap only guarded the completion case, not general concurrent checkpoint
edits — two concurrent `PUT`s to the same draft each merge from their own read, so
the second write to land would silently clobber the first one's fields with no error
to either client. Added a `version` column and made every `update()` a proper
optimistic-concurrency compare-and-swap: read the version, `UPDATE ... AND version =
?` (the value just read), `version = version + 1` on success. Zero affected rows now
means *something* changed since the read — a follow-up lookup distinguishes
"completed" from "edited by another request" so the client gets an accurate,
actionable `409` either way, not a generic one. `sql/table.sql` updated (applied to
the live table via `ALTER TABLE` for this proof, since module install only runs
`CREATE TABLE IF NOT EXISTS`), plus a static test locking in that the version column
is actually used in both the read and the compare-and-swap write.

A third review round found eight more issues (spread across correctness, style, and
maintenance). Three were genuine correctness bugs in the field-validation logic that
would hit normal use of the feature, per the user's explicit direction to fix those
and defer the rest:

- **Fixed:** `accommodation_detail` was enforced as *required* whenever
  `other_accommodation` was selected, contradicting `ONBOARDING_CONTRACT.md` row 9
  ("Detail is optional, limited to 200 characters"). Now only validated (length) if
  supplied; never required. Step 6 above locks this in.
- **Fixed:** a non-array `accommodations` value was silently coerced to `[]` and
  saved, overwriting any previously-saved selection with a `200` and no error. Now
  rejected with `400`. Step 7 above locks this in.
- **Fixed:** checkpointing a new `preferred_contact_method` without resending
  `contact_value` re-validated the *stale* old-method-shaped value against the new
  method, always failing with a misleading format error (e.g. a saved phone number
  checked against the email regex). `update()` now drops the stale `contact_value`
  when the method is changing and no fresh value is supplied in the same request,
  and the error message distinguishes "missing" from "invalid format" so the client
  knows to supply one, not that the old one was malformed. Step 5 above locks this
  in.

**Deferred, not fixed** (accepted as-is for this PR, per explicit user direction):
`parseJsonBody()` accepts a JSON array as well as a JSON object (creates a useless
empty draft instead of `400`); `accommodation_detail`'s 200-character limit is
`strlen()` (bytes) rather than a character count, so non-ASCII text could hit it
early; an array-typed `contact_value` hits a PHP type-coercion warning before
validation runs rather than a clean `400` (severity depends on the live
error-handler config, unconfirmed); no schema-migration mechanism for an
already-installed module (already documented above -- hit during this very
development); and `_b64url()`/`_pkce_pair()` are duplicated verbatim between this
script and `scripts/probe_patient_context.py`.

### Bug found in OpenEMR core along the way

`HttpRestRequest::getRequestBodyJSON()` (`src/Common/Http/HttpRestRequest.php`) calls
`->getContents()` on a raw PHP stream *resource*, which fatals
(`"Call to a member function getContents() on resource"`). This is presumably why no
core route actually uses it — every one of them reads the body via
`json_decode(file_get_contents("php://input"), true)` directly instead
(`apis/routes/_rest_routes_standard.inc.php`). Worked around the same way; not
reported upstream as part of this ticket.

## What this ticket still needs

This closes the platform gap that blocked TICK-017 (`evidence/TICK-001/
ENDPOINT_MATRIX.md`'s "Implementation-blocking API gap" rows for draft
checkpoint/completion). It does not implement TICK-017's guided-onboarding
conversation itself — the LangGraph flow, friction-trigger supportive content
(long pause/upload failure/distress intent), and field-by-field conversational
collection described in `ONBOARDING_CONTRACT.md` are separate, substantial AI-server
work not attempted here.
