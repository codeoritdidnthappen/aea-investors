---
id: TICK-002
title: "spike(portal): select a supported patient-portal iframe hook"
type: spike
epic: EPIC-01
priority: P1
estimate: M
depends_on: [TICK-001]
labels: [openemr, portal, discovery]
source: [FR-1, FR-2, FR-3, FR-4]
status: todo
remote_url: null
---

## Context

The OpenEMR integration must use a supported hook on the pinned release so the patient remains in the portal and browser JavaScript never receives OpenEMR tokens.

## Acceptance Criteria

- [ ] A supported extension hook and installation path are documented for the pinned release.
- [ ] A minimal synthetic-patient proof shows the hook is unavailable when logged out and launches inside the portal when logged in.
- [ ] The proof identifies the direct AI-server callback and confirms the iframe has no OpenEMR API calls.

## Testing

Exercise login, launch, and logout with browser network capture; attach redacted evidence. CI must be green.

## Out of Scope

Implementing the final portal module.
