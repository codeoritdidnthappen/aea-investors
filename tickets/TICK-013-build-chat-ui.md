---
id: TICK-013
title: "feat(chat): deliver accessible streamed patient conversation"
type: feature
epic: EPIC-04
priority: P1
estimate: L
depends_on: [TICK-008, TICK-010, TICK-012]
labels: [chat, frontend, accessibility]
source: [FR-4, FR-18, FR-19, NFR-7, NFR-19]
status: todo
remote_url: null
---

## Context

The iframe is a patient-facing FastAPI-only interface that renders streamed responses and a safe native-scheduler fallback.

## Acceptance Criteria

- [ ] The UI sends turns only to the AI server with the AI-session cookie.
- [ ] Response chunks render progressively and preserve an understandable status.
- [ ] AI-server or LLM unavailability shows instructions for the native OpenEMR scheduler.
- [ ] Desktop controls support keyboard navigation, labels, visible focus, contrast, and non-colour-only status.

## Testing

Add UI tests for streaming and unavailable state plus desktop keyboard and accessibility checks. CI must be green.

## Out of Scope

Android-specific acceptance work or a non-AI scheduling UI.
