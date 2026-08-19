---
id: TICK-014
title: "feat(ocr): process consented synthetic identity uploads locally"
type: feature
epic: EPIC-05
priority: P1
estimate: L
depends_on: [TICK-005, TICK-006, TICK-011]
labels: [ocr, privacy, tesseract]
source: [FR-6, FR-7, FR-21, FR-22, FR-23, FR-25, NFR-23, NFR-29]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/15
builder_commit: 92afe68
---
## Context

OCR is local, consented, and transient: runtime may use only the upload and explicit corrections, never OpenEMR demographics or evaluation labels.

## Acceptance Criteria

- [ ] Explicit consent precedes upload or processing.
- [ ] Malformed, corrupt, oversized, and non-image uploads fail before OCR with clear errors.
- [ ] Pinned local Tesseract extracts only name, date of birth, and address; partial/failure results remain manually completable without fabricated values.
- [ ] Revocation/deletion stops processing and verifiably purges image and extracted transient values; abandoned images expire independently.

## Testing

Test consent, invalid uploads, partial OCR, manual completion, purge, expiry, and runtime isolation from fixtures locally. Run real Tesseract only when installed locally; CI must be green.

## Out of Scope

Cloud OCR, PaddleOCR, or demographic writes.
