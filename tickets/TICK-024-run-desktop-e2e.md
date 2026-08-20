---
id: TICK-024
title: "task(verification): run desktop Chrome critical-flow coverage"
type: task
epic: EPIC-08
priority: P1
estimate: L
depends_on: [TICK-013, TICK-015, TICK-016, TICK-017, TICK-031, TICK-023, TICK-032, TICK-033]
labels: [e2e, chrome, verification]
source: [FR-1, FR-2, FR-6, FR-12, FR-14, FR-18, FR-19, NFR-18, NFR-19, NFR-35]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/25
blocked_reason: "Attempted live 2026-08-20 with real desktop Chrome (not a sandboxed worker) -- login and portal launch verified, but two real bugs found along the way block the remaining flow: TICK-032 (no dashboard nav tile for AI Chat) and TICK-033 (the AI server's registered OAuth client requests staff user/* scopes, not patient/* -- forces a staff-style consent screen a real patient flow can't complete). See evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md. Re-attempt once both land."
---

## Context

Desktop Chrome is the v1 priority and must prove the integrated portal, onboarding, OCR, scheduling, and fallback flow.

**Dependency changed (2026-08-20):** was `TICK-020`; that ticket split into
TICK-031 (book + cancel, buildable) and a narrowed TICK-020 (reschedule only,
permanently blocked -- no OpenEMR service method exists). "Appointment
operations" coverage here means book/cancel; reschedule has no capability to
exercise.

**Attempted, partially blocked (2026-08-20):** ran this live with real desktop
Chrome browser automation. Verified login and portal launch; the chat/
onboarding/OCR/appointment flows themselves couldn't be reached because of two
real, live bugs found in the process -- see `evidence/TICK-024/
DESKTOP_E2E_EVIDENCE.md`. Filed as TICK-032 and TICK-033, now blocking
dependencies. Not a testing-tool limitation: both reproduce reliably and are
documented with enough detail to fix directly.

## Acceptance Criteria

- [ ] Synthetic-patient E2E coverage exercises login, iframe launch, session, streaming, onboarding, OCR confirmation, appointment operations, and fallback.
- [ ] Keyboard and baseline accessibility checks pass on the embedded chat.
- [ ] Failures capture reproducible evidence without protected values.

## Testing

Run the critical-flow suite against the local Docker topology in current stable desktop Chrome. CI must be green.

## Out of Scope

Other desktop browser families.
