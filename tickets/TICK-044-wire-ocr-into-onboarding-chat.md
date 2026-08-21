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

## Design decision (locked in; do not re-litigate)

Reuse the existing chat-message pipe instead of adding a new HTTP route,
upload endpoint, or auth scope -- every other onboarding field already
travels as a JSON-shaped chat message parsed by `_parse_value()`
(`preferred_contact`, `visit_preference`, `accommodations`); an upload is
just another shaped message on the same `POST /api/chat` turn.

1. **Frontend** (`ai_server/app/chat.py`'s `CHAT_PAGE_HTML`): add a file
   input (`accept="image/png,image/jpeg"`) and a small "Attach ID photo"
   control next to the existing message form. When a file is chosen, JS
   base64-encodes it and sends it as the chat message body:
   `{"action": "upload_identity_document", "consent": true, "image_base64": "<base64>"}`.
   This bypasses the textarea's `maxlength="4000"` (send directly via
   `fetch`, not through the textarea value) -- confirm no request-body size
   limit in `deploy/local/Caddyfile` blocks an up-to-8MB upload (matching
   `ai_server/ocr/service.py`'s own `MAX_UPLOAD_BYTES`).
2. **Trigger point**: the upload offer/control is only meaningful (and
   should only be presented/accepted) when the onboarding flow is at
   `next_field == "given_name"` with no identity field yet answered -- the
   same point `FIELD_PROMPTS["given_name"]` is first shown. Update that
   prompt text to mention the upload option.
3. **`OnboardingChatService` handling**: recognize the
   `upload_identity_document` action at that point. Refuse (per FR-21,
   `OcrService.begin`'s own `ConsentRequiredError`) unless `consent: true`
   is explicitly present. Decode the base64, call
   `OcrService.begin(consent=True, now)` then
   `submit(upload_id, image_bytes, now)`. Handle
   `UnsupportedImageFormatError`/`CorruptImageError`/`OversizedUploadError`
   (FR-22) and a `TesseractUnavailableError`-driven empty result (FR-7)
   with a clear rejection message that lets the patient retry the upload or
   fall back to typing -- never crash the turn.
4. **Confirmation, not auto-apply**: on successful extraction, walk the
   patient through the *same* `given_name`/`family_name`/`date_of_birth`/
   `address` prompts already in `FIELD_PROMPTS`, each prefixed with the
   extracted value as a suggestion (e.g. "We read your given name as
   'Avery' from your upload. Reply with that name to confirm, or type a
   correction."). `ExtractedIdentity.name` (a single combined field, see
   `ai_server/ocr/service.py`) has no given/family split -- show it once as
   a hint for both name prompts; the patient still types every field
   themselves. Nothing is written to `state.identity` until the patient's
   own typed reply passes the *existing* `validate_field` path -- no new
   validation logic, no path from an unconfirmed extracted value to a write
   (preserves FR-23/TICK-016 AC1 exactly as already enforced downstream by
   TICK-042).
5. **Purge**: call `OcrService.delete(upload_id, now)` immediately after
   the extracted values are read into the prompts -- a single-use hint,
   matching NFR-23. Nothing about the image or extraction persists past
   that turn; revocation mid-flow (the patient declines before finishing)
   must also purge (`OcrService.revoke`).
6. **Fully optional**: a patient who never attaches a file sees exactly
   today's flow, unchanged -- this is additive only, not a new required
   step. `test_full_onboarding_conversation_reaches_a_completed_record`
   (`ai_server/tests/test_onboarding_chat.py`) must keep passing unmodified
   as the no-upload path.

## Acceptance Criteria

- [ ] A patient can, through the real chat UI, attach a synthetic ID image,
      consent, and see the OCR-extracted fields offered as suggestions on
      the same given/family/DOB/address prompts already in `FIELD_PROMPTS`
      -- proven live (real Tesseract, a real fixture image), not just
      mocked in a unit test.
- [ ] Only the patient's own typed confirmation/correction reaches
      `state.identity` -- an extracted-but-unconfirmed value has no path to
      a write, verified by a test that submits an upload and then replies
      with a *different* value than what was extracted, asserting the
      corrected value (not the extracted one) is what gets written.
- [ ] Malformed/oversized/non-image uploads and a Tesseract-unavailable
      empty result (TICK-014's own ACs) are reachable and produce a clear,
      non-crashing rejection through the chat surface.
- [ ] The upload's image and extracted values are purged
      (`OcrService.delete`/`revoke`) and provably inaccessible after the
      turn that consumes them -- test that a second `identity()`/`image()`
      call on the same `upload_id` returns `None`.
- [ ] The no-upload path is unchanged: existing onboarding tests keep
      passing without modification.
- [ ] `TICK-024`'s own "OCR confirmation" acceptance criterion can finally
      be exercised live once this lands.

## Testing

Live verification against the local Docker topology through the real chat
UI (a real synthetic fixture image, real local Tesseract), plus unit tests
for consent refusal, each `InvalidUploadError` subtype, Tesseract failure,
purge-after-use, and the confirm-vs-correct path. CI must be green.

## Out of Scope

Any change to `OcrService`'s own local-Tesseract extraction logic, accuracy
gate, or purge/expiry mechanics (TICK-014/015, already done and unaffected).
Cloud OCR or any non-Tesseract engine. A dedicated multipart/REST upload
endpoint (the design above deliberately reuses the existing chat-message
pipe instead).
