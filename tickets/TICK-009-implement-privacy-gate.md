---
id: TICK-009
title: "feat(privacy): enforce local outbound PHI and PII rejection"
type: feature
epic: EPIC-03
priority: P1
estimate: L
depends_on: [TICK-005, TICK-006]
labels: [privacy, presidio, security]
source: [NFR-2, NFR-3, NFR-4, NFR-5, NFR-8, NFR-27, NFR-28]
status: todo
remote_url: null
---

## Context

Every external model request must cross a pinned local Presidio gate. Matches are rejected locally, never scrubbed and forwarded.

## Acceptance Criteria

- [ ] A pinned local Presidio Analyzer uses built-in PII/medical recognizers plus custom OpenEMR, MRN, and healthcare recognizers.
- [ ] Any match returns a local correction response and makes no external request.
- [ ] The allowlist validates the approved outbound payload shape before dispatch.
- [ ] The fixture-complete golden corpus blocks deployment on any allowed seeded sensitive value.
- [ ] No cloud detection service, prompt text, or recognition result enters logs.

## Testing

Run golden reject/allow tests with a capturable fake model client and log-redaction tests. CI must be green.

## Out of Scope

LLM prompting or OpenEMR operations.
