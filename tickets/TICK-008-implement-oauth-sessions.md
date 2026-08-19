---
id: TICK-008
title: "feat(auth): implement OAuth launch and durable AI sessions"
type: feature
epic: EPIC-03
priority: P1
estimate: L
depends_on: [TICK-001, TICK-005]
labels: [auth, security, openemr]
source: [FR-1, FR-3, NFR-6, NFR-7, NFR-10, NFR-30, NFR-31, NFR-32, NFR-33]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/9
---

## Context

OpenEMR must delegate authorization directly to the AI server, which retains only encrypted tokens and non-patient session plumbing across restart.

## Acceptance Criteria

- [ ] OAuth/SMART code exchange uses validated state, nonce, PKCE, and the scopes proven by TICK-001.
- [ ] Replay, stale state, and nonce mismatch fail closed.
- [ ] The browser receives only a secure HttpOnly AI-session cookie.
- [ ] SQLite WAL persists hashed handles, expiry, non-patient cursor, and AES-256-GCM-encrypted tokens with unique nonce and authenticated metadata.
- [ ] Expiry deletes the session and encrypted tokens; no patient record or prompt is stored.

## Testing

Unit-test cryptography and state validation; integration-test callback, restart recovery, and expiry using synthetic credentials. CI must be green.

## Out of Scope

Portal embedding or patient-record operations.
