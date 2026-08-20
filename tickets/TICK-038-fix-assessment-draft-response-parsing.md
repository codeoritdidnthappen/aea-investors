---
id: TICK-038
title: "bug(onboarding): AI server rejects OpenEMR's own successful assessment-draft response"
type: task
epic: EPIC-03
priority: P1
estimate: S
depends_on: [TICK-017, TICK-035, TICK-037]
labels: [onboarding, ai-server, openemr]
source: [FR-8, FR-27]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/75
builder_commit: 8391156
---
## Context

Found live 2026-08-20 immediately after TICK-037's fix, running onboarding
end-to-end for the first time with a genuinely working access token. The
scope bug is gone -- OpenEMR now returns a real `201` for
`POST /apis/dispatch.php/default/portal/patient/assessment`, and a correct
`draft` row is written to `aeai_assessment_draft` for the right
`patient_uuid` (confirmed directly in the database). But the chat UI still
shows "Chat unavailable", and the AI server logs:

```
File "/app/ai_server/onboarding/draft_client.py", line 83, in create
    return _draft_from_response(response)
File "/app/ai_server/onboarding/draft_client.py", line 170, in _draft_from_response
    raise OpenEmrRequestError("OpenEMR returned an invalid assessment draft response")
```

`AssessmentDraftAdapter.create()` only reaches `_draft_from_response()` after
checking `response.status_code != 201` (`draft_client.py:81`), so the status
really was 201 -- this isn't a disguised error response. `_draft_from_response()`
calls `_safe_json(response)`, which swallows `ValueError` from `response.json()`
and returns `None`; since `payload` isn't a `dict`, the adapter raises its
generic "invalid response" error. So either the response body isn't valid
JSON by the time httpx parses it, or it's valid JSON but not shaped as
`{"uuid": str, "status": str, "fields": dict}`.

`AssessmentDraftService::create()` (the OpenEMR-side module code building the
response) returns exactly
`new JsonResponse(['uuid' => $uuid, 'status' => 'draft', 'fields' => $fields], 201)`
-- the right shape, matching `draft_client.py`'s expectations exactly. Given
the database row is correct, this PHP code path did run successfully and did
build the intended body. The mismatch must be happening somewhere between
that `JsonResponse` object and what `httpx.Response.json()` sees on the AI
server side: e.g. something else at the OpenEMR REST framework layer
(`RoutesExtensionListener`/`HttpRestRouteHandler`) altering, wrapping, or
appending to the body after the controller returns it, a stray PHP
warning/notice leaking into the response stream, a `Content-Type` mismatch
that trips up httpx's auto-detection, or a Caddy-layer issue specific to
this route. Not yet isolated -- needs the raw response bytes captured
directly (e.g. a temporary debug log of `response.text` and
`response.headers` in `draft_client.py`, or a raw `curl` against the route
with a real bearer token) before attempting a fix.

## Acceptance Criteria

- [ ] Root cause confirmed with direct evidence (the actual raw response
      bytes/headers that fail to parse), not just the hypotheses above.
- [ ] A genuine patient login through `/oauth/launch`, followed by a real
      onboarding-start chat turn, completes without error and the assistant
      responds with the next onboarding question -- proven live.
- [ ] Fix does not touch a core OpenEMR file in place (same standing
      constraint as TICK-037); if the cause turns out to be inside OpenEMR's
      own REST framework, prefer an AI-server-side accommodation over a
      vendor patch unless no such accommodation is possible.

## Testing

Live verification against the local Docker topology: a real
`authorization_code`+PKCE patient login, a real onboarding-start chat turn,
through to a visible assistant response (not just a non-error HTTP status).
CI must be green.

## Out of Scope

Any other onboarding step past the first draft-creation call. Scheduling/
booking (TICK-034, unaffected -- does not use this adapter).
