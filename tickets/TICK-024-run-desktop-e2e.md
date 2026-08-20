---
id: TICK-024
title: "task(verification): run desktop Chrome critical-flow coverage"
type: task
epic: EPIC-08
priority: P1
estimate: L
depends_on: [TICK-013, TICK-015, TICK-016, TICK-017, TICK-031, TICK-023, TICK-032, TICK-033, TICK-038, TICK-039]
labels: [e2e, chrome, verification]
source: [FR-1, FR-2, FR-6, FR-12, FR-14, FR-18, FR-19, NFR-18, NFR-19, NFR-35]
status: blocked
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/25
blocked_reason: "Re-attempted live 2026-08-20 now that TICK-032/033 landed -- got substantially further (nav tile, patient-context consent, streaming, accessibility, and keyboard operability all verified live) but two new, real, live bugs found along the way block full closure: TICK-038 (onboarding's first turn reaches a real successful OpenEMR write but the AI server fails to parse the response) and TICK-039 (cancellation never selects a real, available appointment despite it reaching the LLM's context). See evidence/TICK-024/DESKTOP_E2E_EVIDENCE_2.md. Re-attempt once both land."
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

**Re-attempted, partially blocked again (2026-08-20):** TICK-032/033 landed;
re-ran live and got much further -- dashboard nav tile, patient-context
consent, session, streaming, accessibility (semantic roles + full keyboard
operability), and the honest no-availability booking response all verified.
Two new real bugs found and filed (TICK-038, TICK-039), now blocking the
remaining onboarding/OCR and cancellation-completion coverage -- see
`evidence/TICK-024/DESKTOP_E2E_EVIDENCE_2.md`.

## Acceptance Criteria

- [ ] Synthetic-patient E2E coverage exercises login, iframe launch, session, streaming, onboarding, OCR confirmation, appointment operations, and fallback.
- [ ] Keyboard and baseline accessibility checks pass on the embedded chat.
- [ ] Failures capture reproducible evidence without protected values.

## Testing

Run the critical-flow suite against the local Docker topology in current stable desktop Chrome. CI must be green.

## Out of Scope

Other desktop browser families.
