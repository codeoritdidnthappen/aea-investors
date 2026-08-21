---
id: TICK-044
title: "feat(onboarding): wire consented OCR identity upload into the chat flow"
type: feature
epic: EPIC-05
priority: P2
estimate: L
depends_on: [TICK-014, TICK-015, TICK-016, TICK-035, TICK-042]
labels: [onboarding, ocr, chat]
source: [FR-6, FR-7, FR-21, FR-22, FR-23, FR-25, NFR-23, NFR-29]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/90
---
## Context

Found live 2026-08-20 while re-verifying TICK-024's desktop E2E coverage,
now that onboarding actually completes end to end (TICK-042). Two backend
capabilities were each built and independently tested, but were never
connected to each other or to the live chat surface:

- TICK-014/015/016 (`ai_server/ocr/`) implement consented, local Tesseract
  identity-document OCR, an accuracy gate, and confirmed-only demographic
  persistence -- all `status: done`, all with their own passing test suites.
- TICK-017/035 (`ai_server/onboarding/`, `ai_server/app/onboarding_chat.py`)
  implement guided onboarding -- but its identity capture
  (`FIELD_PROMPTS["given_name"|"family_name"|"date_of_birth"|"address"]`,
  `onboarding_chat.py:66-93`) asks the patient to *type* each field directly.
  There is no upload step, no reference to `ai_server.ocr` anywhere in
  `ai_server/app/*.py` (confirmed by grep), and no HTTP route exposes an
  upload endpoint (`ai_server/app/main.py` registers no OCR-related route).

TICK-024's own acceptance criteria ("...exercises login, iframe launch,
session, streaming, onboarding, OCR confirmation, appointment operations,
and fallback") assumes OCR confirmation is reachable as part of the live
onboarding conversation. It structurally is not: `ExtractedIdentity`
(`ai_server/ocr/service.py`) is imported only by
`ai_server/tests/test_openemr_demographics.py` and
`ai_server/tests/test_ocr_service.py` -- never by any code path a real
request can reach.

This is not a bug fixable by a small correction; it is two finished,
independently-tested features that were never integrated. Filed as its own
ticket rather than folded into TICK-024 (a verification ticket, not a
build ticket) or silently built ad hoc, since the integration shape itself
is a product decision this ticket's Acceptance Criteria intentionally
leaves open rather than presupposing.

## Acceptance Criteria

- [ ] Decide and document the integration shape: is a document upload an
      alternative to typing identity fields (patient's choice), a
      preceding step that pre-fills the typed fields for confirmation
      (TICK-016's "every extracted field is shown for confirmation or
      correction before a write is available"), or a separate, later
      in-portal flow outside this chat entirely? `ONBOARDING_CONTRACT.md`
      does not currently specify this and should be updated to match
      whatever is decided.
- [ ] A patient can, through the real chat UI, consent to and complete an
      identity-document upload, see the OCR-extracted fields for
      confirmation/correction (never auto-applied), and have only the
      confirmed values flow into the same `write_confirmed_demographics()`
      path TICK-042 made reachable -- proven live, not just unit-tested.
- [ ] Malformed/oversized/non-image uploads and revocation (TICK-014's own
      ACs) are reachable and behave correctly through the chat surface, not
      only through `OcrService`'s own isolated test suite.
- [ ] `TICK-024`'s own "OCR confirmation" acceptance criterion can finally
      be exercised live once this lands.

## Testing

Live verification against the local Docker topology through the real chat
UI, plus unit/integration tests for the new chat-facing wiring code. CI must
be green.

## Out of Scope

Any change to `OcrService`'s own local-Tesseract extraction logic, accuracy
gate, or purge/expiry mechanics (TICK-014/015, already done and unaffected).
Cloud OCR or any non-Tesseract engine.
