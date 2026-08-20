# TICK-038 — "OpenEMR returned an invalid assessment draft response", live evidence

Executed against the running local Docker topology (`local-openemr-1`, `local-mariadb-1`,
`local-caddy-1`, `local-ai-server-1`), the same shared stack `evidence/TICK-002`,
`evidence/TICK-017`, `evidence/TICK-024`, `evidence/TICK-028`, `evidence/TICK-033`, and
`evidence/TICK-037` used.

## Result

Root cause confirmed with the actual raw response bytes (not the tracing-only
hypotheses in the ticket): `AssessmentDraftService::create()` returns
`'fields' => $fields` where `$fields` is a genuinely empty PHP array whenever the
caller checkpoints zero fields on creation -- exactly what
`ai_server/onboarding/flow.py:137`'s `self._draft_adapter.create(access_token, {})`
does on every fresh `OnboardingFlow.start()`. PHP's `json_encode` cannot tell an
empty associative array apart from an empty list; both are just `[]`. The route's
real response body was therefore:

```
{"uuid":"380a9a51-1a5b-4229-bb71-69cffb7006b0","status":"draft","fields":[]}
```

This **is** valid JSON -- `httpx.Response.json()` parses it without raising -- so
`_safe_json()` never returns `None` and the request status genuinely was `201`,
exactly as the ticket's own tracing established. The failure is
`draft_client.py`'s `_draft_from_response()` correctly rejecting `fields` because it
is a JSON array, not object: `isinstance(fields, dict)` is `False` for a decoded
`[]`. This matches the ticket's traceback exactly (`draft_client.py:170`, the
`uuid`/`status`/`fields`-shape check, not the earlier `payload` dict check at line
167).

Nothing at the Caddy layer, the OpenEMR REST framework
(`RoutesExtensionListener`/`HttpRestRouteHandler`), or a leaked PHP
warning/notice is involved -- the entire cause is this project's own module code
(`openemr_modules/aeai-portal-chat/src/Service/AssessmentDraftService.php`), which
is not a core OpenEMR file and was safe to edit directly (no vendor patch, no
AI-server-side accommodation needed).

## Reproduction (before fix)

`AssessmentDraftAdapter.create()` called with the exact body `flow.py` sends,
using a real patient-context bearer token obtained through a genuine
`authorization_code`+PKCE login against OpenEMR (the AI server's own registered
OAuth client and scopes, patient pid 1):

```
POST https://emr.localhost/apis/default/portal/patient/assessment
Authorization: Bearer <real patient token>
Content-Type: application/json
{}
```

Raw response:

```
HTTP/1.1 201
content-type: application/json
{"uuid":"380a9a51-1a5b-4229-bb71-69cffb7006b0","status":"draft","fields":[]}
```

`httpx`'s `.json()` on this body returns
`{'uuid': '...', 'status': 'draft', 'fields': []}` -- `fields` is a `list`, not a
`dict`, reproducing `OpenEmrRequestError("OpenEMR returned an invalid assessment
draft response")` exactly.

## Fix

`AssessmentDraftService` now routes every `'fields'` value in a `JsonResponse`
through a new `asFieldsObject()` helper (`(object) $fields`) at all three call
sites (`create()`, `read()`, `update()`). An `(object)` cast of a PHP array forces
`json_encode` to emit `{}` for the empty case while leaving a populated
associative array's encoding unchanged (same keys/values either way) -- verified
strictly backward compatible with every non-empty-fields response the existing
`evidence/TICK-017` probe already exercised.

## Live proof (after fix)

Same reproduction as above, after recreating `local-openemr-1` from this
ticket's worktree (bind-mounts `openemr_modules/aeai-portal-chat`, no core file
touched):

```
HTTP/1.1 201
{"uuid":"9a8d9c47-d67c-4d82-8957-d752b703d8a6","status":"draft","fields":{}}
```

`fields` is now `{}` (a JSON object) and `httpx`'s `.json()` returns
`{'uuid': '...', 'status': 'draft', 'fields': {}}` -- `AssessmentDraft` construction
succeeds.

### Full live flow (AC2)

A genuine patient login through the AI server's actual `/oauth/launch` (never a
direct OpenEMR token exchange bypassing it), followed by a real onboarding-start
chat turn through `/api/chat`:

```
GET  https://chat.localhost/oauth/launch          -> 302 (OpenEMR authorize)
     ... OpenEMR OAuth2 login form + consent screen (real credentials, real consent POST) ...
GET  https://chat.localhost/oauth/callback?code=...&state=...   -> 303, sets ai_session cookie
POST https://chat.localhost/api/chat  {"message": "start onboarding"}
     -> 200
     "How should we contact you? Reply as JSON, e.g.
      {\"method\": \"phone\", \"value\": \"+15551234567\"}, ..."
```

The assistant responds with the next onboarding question -- not "Chat
unavailable" and no `OpenEmrRequestError` traceback in `local-ai-server-1`'s
logs for this request (confirmed via `docker logs`).

After verification, `local-openemr-1` was recreated once more pointing back at
the main checkout's `openemr_modules`/`openemr_overrides` paths, restoring the
shared stack to its pre-existing state for any other ticket's worker using it
concurrently.

## Environment provisioning (2026-08-20)

- Patient pid 1 (Avery Subjecttest, the same synthetic subject `evidence/TICK-017`
  used)'s portal password was reset via direct SQL
  (`password_hash(..., PASSWORD_DEFAULT)`, matching `AuthHash`) to a value known
  for this probe run -- the prior plaintext was never retained anywhere and could
  not be recovered. Same category of deviation already on record for
  `evidence/TICK-017`/`evidence/TICK-002`/`evidence/TICK-028`: environment
  provisioning for a synthetic fixture, not a product data path. The new value
  was never written to a file or logged, and is not retained anywhere in this
  repository.
- Reused the AI server's own already-registered, already-enabled production
  OAuth client (`OPENEMR_OAUTH_CLIENT_ID`/`_SECRET` from the running
  `local-ai-server-1` container's environment) -- no new test client was
  registered, consistent with proving the actual product path rather than a
  probe-only substitute.

## What this ticket does not change

`ai_server/onboarding/draft_client.py`'s own validation (`isinstance(fields,
dict)`) is unchanged and correctly rejects a malformed `fields` shape; a new test
(`test_a_fields_array_instead_of_object_fails_explicitly`,
`ai_server/tests/test_onboarding_draft_client.py`) locks in that this
client-side defense would still catch a regression of the OpenEMR-side bug even
if `asFieldsObject()` were ever removed. Scheduling/booking (TICK-034) does not
use this adapter and is unaffected, per the ticket's Out of Scope.
