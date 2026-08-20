---
id: TICK-012
title: "feat(portal): embed the authenticated chat iframe"
type: feature
epic: EPIC-04
priority: P1
estimate: M
depends_on: [TICK-002, TICK-008]
labels: [openemr, portal, frontend]
source: [FR-1, FR-2, FR-3, FR-4, NFR-6, NFR-7]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/13
builder_commit: 046067c
---
## Context

The selected supported patient-portal hook launches the chat without navigating away and maintains browser isolation from OpenEMR APIs.

## Acceptance Criteria

- [ ] Only an authenticated patient can see and launch the portal entry.
- [ ] The entry renders the chat inside the OpenEMR portal using the selected hook.
- [ ] The iframe communicates only with the AI server; browser network capture shows no OpenEMR API request from it.

⚠️ **Verification gap, on the record (2026-08-20):** `ai_server/tests/test_portal_module.py`
only asserts against the committed module source and its docker-compose wiring (no
`/apis/`, `/oauth2/`, `fhir`, `Bearer`, `fetch(`, or `XMLHttpRequest` string in the PHP
source; correct read-only mount and env passthrough). Unlike TICK-002's curl-based
proof, no request was made against a running OpenEMR container with the module actually
installed and enabled — these three criteria are therefore unverified at runtime, not
just unchecked bookkeeping. Live verification belongs with TICK-024's desktop Chrome
critical-flow pass once its remaining blocker (TICK-031; TICK-017 is done, and TICK-024
no longer depends on TICK-020, which narrowed to a permanently blocked reschedule-only
scope) resolves; it was not attempted here to keep this pass scoped to the code-review
merge gate.

## Testing

Run synthetic-patient login/launch/logout browser tests against the local OpenEMR stack and inspect local browser network requests. CI must be green.

## Out of Scope

Staff, provider, office, or admin AI chat.
