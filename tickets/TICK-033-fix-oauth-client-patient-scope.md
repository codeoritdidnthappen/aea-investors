---
id: TICK-033
title: "bug(auth): re-register the AI server's OAuth client with patient-context scopes"
type: task
epic: EPIC-03
priority: P1
estimate: M
depends_on: [TICK-008]
labels: [auth, security, openemr]
source: [FR-3, NFR-25]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/64
builder_commit: be2070b
---
## Context

Found live during TICK-024's real desktop Chrome E2E pass
(`evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md`, finding 2). The AI server's
actual configured OAuth client (confirmed from `local-ai-server-1`'s own
environment) is:

```
client_name: Intake Assistant (TICK-025 local)
client_role: user
scope: openid api:oemr user/patient.crus user/appointment.cruds
```

Every scope is `user/*` (staff-context), not `patient/*`. Not a new
discovery -- `evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md` already recorded this
exact observation about the AI server's registered client and explicitly
flagged it as out of scope for TICK-002 at the time. This ticket is that
follow-up, now confirmed still live and blocking real E2E verification: since
this client is `user`-role, OpenEMR's OAuth2 provider does not treat it as
eligible for "Patient standalone apps Auto Approved" -- a genuine patient
login hits a full staff-style resource-permission consent screen, which a
patient should never see for a scheduling chat assistant, and which contradicts
the product's whole authorization-boundary premise (patient-context, per
`ARCHITECTURE.md` and TICK-028's binding work).

## Acceptance Criteria

- [ ] The AI server's registered OAuth client requests only `patient/*`
      scopes appropriate to what it actually needs (chat, scheduling,
      assessment-draft, demographics-read), not `user/*`.
- [ ] A genuine patient login through the real `/oauth/launch` flow reaches
      the AI server's chat page without a manual staff-style consent screen
      (either because the client is patient-role/auto-approved, or because
      whatever consent screen remains is a patient-appropriate one).
- [ ] `deploy/local/.env(.example)` and any registration documentation are
      updated to match the corrected client configuration.
- [ ] The change is proven with a live login, not just a registration-row
      inspection: a real patient token is obtained through the real flow.

## Testing

Live verification against the local Docker topology: real
`authorization_code`+PKCE patient login through `/oauth/launch`, confirming
no staff-style consent screen and a successful `ai_session` cookie. CI must be
green.

## Out of Scope

Re-attempting TICK-024's remaining E2E cases (separate ticket, depends on
this one landing first).
