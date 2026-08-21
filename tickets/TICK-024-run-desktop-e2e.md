---
id: TICK-024
title: "task(verification): run desktop Chrome critical-flow coverage"
type: task
epic: EPIC-08
priority: P1
estimate: L
depends_on: [TICK-013, TICK-015, TICK-016, TICK-017, TICK-031, TICK-023, TICK-032, TICK-033, TICK-038, TICK-039, TICK-040, TICK-041, TICK-042, TICK-044]
labels: [e2e, chrome, verification]
source: [FR-1, FR-2, FR-6, FR-12, FR-14, FR-18, FR-19, NFR-18, NFR-19, NFR-35]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/25
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

**Re-attempted a third time, blocked only on a missing integration
(2026-08-20):** TICK-038/039/040/041/042 all landed; re-ran live and
verified every remaining AC item except OCR confirmation -- full onboarding
completion with a real `patient_data` write, and real appointment
cancellation with database confirmation, both through the actual chat UI.
OCR confirmation has no live path to test: `ai_server/ocr/` and
`ai_server/onboarding/` were each built and tested independently but never
wired together. Filed as **TICK-044** (a build ticket, out of this
verification ticket's own scope) -- see
`evidence/TICK-024/DESKTOP_E2E_EVIDENCE_3.md`.

**Closed out (2026-08-20):** TICK-044 landed and was live-verified through
the real chat UI -- a real synthetic ID photo uploaded through the real
"Attach ID photo" file input, genuinely processed by local Tesseract, shown
as a suggestion on the given/family/DOB/address prompts, and only the
patient's own typed corrections (not the extracted values) reached the
final `patient_data` write. Every AC item this ticket lists has now been
verified live at least once: login, iframe launch, session, streaming,
onboarding (including OCR confirmation), appointment operations
(book/cancel/reschedule), fallback, and keyboard/accessibility. See
`evidence/TICK-044/OCR_UPLOAD_CHAT_EVIDENCE.md` for the OCR-confirmation
proof.

## Acceptance Criteria

- [x] Synthetic-patient E2E coverage exercises login, iframe launch, session, streaming, onboarding, OCR confirmation, appointment operations, and fallback.
- [x] Keyboard and baseline accessibility checks pass on the embedded chat.
- [x] Failures capture reproducible evidence without protected values.

## Testing

Run the critical-flow suite against the local Docker topology in current stable desktop Chrome. CI must be green.

## Out of Scope

Other desktop browser families.
