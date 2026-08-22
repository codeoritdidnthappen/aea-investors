---
id: TICK-055
title: "bug(auth): no logout exists, so portal sign-out leaves a live chat session holding the patient's token"
type: task
epic: EPIC-03
priority: P1
estimate: M
depends_on: [TICK-008]
labels: [auth, privacy, chat, bug]
source: [FR-1, NFR-31]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/110
builder_commit: bfd6650
---
## Context

Found while tracing the portal sign-in flow for TICK-051 (2026-08-22). The
AI server exposes exactly five routes (`ai_server/app/main.py`):

```
/health   /   /api/chat   /oauth/launch   /oauth/callback
```

There is no logout, no revocation, and no `delete_cookie` anywhere in
`ai_server/` or `openemr_modules/`. Three consequences, each verified:

**1. Portal sign-out does not end the chat session.** OpenEMR's portal
logout ends its own PHP session. It cannot touch the `ai_session` cookie,
which is scoped to `chat.localhost` (a different origin), and nothing tells
the AI server anything happened. The session row -- holding the patient's
AES-GCM-encrypted OpenEMR access and refresh tokens -- stays valid for the
remainder of its TTL. Navigating straight to `https://chat.localhost/`
after "logging out" resumes chatting as that patient against a live token.
On a shared machine that is a patient-data exposure, and it defeats FR-1
("an unauthenticated user cannot open or use the chat") for anyone who
reasonably believes logging out logged them out.

**2. The session TTL is absolute and long.** `session_ttl` is 8 hours
(`ai_server/app/auth.py:50`). `create_session` stamps `expires_at = now +
ttl` once (`auth.py:398-421`) and nothing ever extends or shortens it --
`save_cursor` updates the cursor `WHERE expires_at > ?` but never touches
the expiry. So the exposure window above is up to 8 hours regardless of
activity, and separately a patient mid-conversation is cut off at the
8-hour mark with no renewal, even though a refresh token is stored
(`auth.py:402-427`) and no code path ever redeems it.

**3. Expired sessions are never swept.** `SessionStore.purge_expired()`
(`auth.py:509-517`) is never called anywhere in `ai_server/app/`. Only
`active_session()` lazily deletes a row, and only when the handle is
presented after expiry. The `ai_session` cookie carries no `max_age` or
`expires` (`main.py:259-272`), so it is a browser-session cookie: closing
the browser discards it, the handle is never presented again, and the row
with its encrypted tokens is retained in SQLite indefinitely. NFR-31 governs
what that store holds; nothing currently bounds how long it holds it.

## Acceptance Criteria

- [ ] A logout path exists that ends an AI session: the stored row is
      deleted (not merely marked expired) and the `ai_session` cookie is
      cleared on the response.
- [ ] Signing out of the OpenEMR portal ends the AI session too. Whichever
      mechanism is chosen -- OIDC RP-initiated logout against the client's
      registered `post_logout_redirect_uris` (already present in the
      registration, `deploy/local/PATIENT_AUTH.md:82`), a portal-module hook
      calling the new endpoint, or another -- the choice and its failure
      modes are recorded in the ticket. A logout that silently no-ops is a
      failure, not a degradation.
- [ ] After logout, `GET /` and `POST /api/chat` do not resume the previous
      patient's session. Verified by navigating directly to the chat origin
      after signing out of the portal, not only by unit test.
- [ ] Logout is not a CSRF sink: an off-origin request cannot log a patient
      out. It reuses the same `Origin` discipline `chat_turn` applies
      (`main.py:202`), and the settings split TICK-051 introduces.
- [ ] `purge_expired()` is actually called on a schedule or at startup, so
      rows for sessions that are never revisited do not accumulate. A
      committed test asserts an abandoned expired session is removed without
      its handle ever being presented again.
- [ ] Whether the 8-hour absolute TTL stays absolute is decided explicitly
      and recorded -- either the stored refresh token is redeemed to extend
      an active session, or the ticket states that a hard 8-hour cut is
      intended and the patient is warned before it lands.

## Testing

Unit tests for the logout path (row deleted, cookie cleared, subsequent
`/api/chat` 401s, off-origin logout refused) and for the sweep (an expired
row is purged with no read of its handle).

Then live verification against the local Docker topology with real desktop
Chrome: sign in as a seeded synthetic patient, hold a chat turn, sign out of
the portal, then navigate directly to the chat origin and confirm the
session is gone rather than resumable. Record under `evidence/TICK-055/`.
CI must be green.

Note the hazard recorded for this repo: `ai-server` is not bind-mounted, so
rebuild with `--build` before verifying.

## Out of Scope

Where a sign-in lands the patient (TICK-051) and deferring the panel's
launch (TICK-054). Revoking the delegated token at OpenEMR's end, if the
release exposes no revocation endpoint -- if it does not, that is recorded
as a finding rather than worked around.
