---
id: TICK-015
title: "task(ocr): gate release on golden-set accuracy"
type: task
epic: EPIC-05
priority: P1
estimate: M
depends_on: [TICK-006, TICK-014]
labels: [ocr, evaluation, release-gate]
source: [NFR-29]
status: todo
remote_url: null
---

## Context

Tesseract is the sole v1 OCR engine and must meet the documented field-level target before deployment.

## Acceptance Criteria

- [ ] The evaluator runs the isolated synthetic-ID golden set against pinned Tesseract and trained data.
- [ ] Field-level accuracy is calculated for name, date of birth, and address.
- [ ] Deployment is blocked below 90%; the report explicitly reopens the OCR-engine decision rather than adding another engine.

## Testing

Test metric calculation and threshold behavior with passing and failing sample sets. CI must be green.

## Out of Scope

Changing the 90% target or adding an OCR engine.
