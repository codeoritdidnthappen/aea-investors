---
id: TICK-006
title: "feat(fixtures): generate paired synthetic patient and ID data"
type: feature
epic: EPIC-02
priority: P1
estimate: L
depends_on: [TICK-005]
labels: [fixtures, ocr, privacy]
source: [FR-24, FR-25, NFR-1, NFR-24, NFR-28, NFR-29]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/7
---

## Context

Every demo patient, seed record, identity image, expected OCR label, and privacy corpus value must derive from a unique synthetic identity while runtime remains isolated from test answers.

## Acceptance Criteria

- [ ] An offline generator produces paired synthetic OpenEMR seeds and identity images from one source identity per patient.
- [ ] OCR labels and privacy corpus values are generated only for evaluation.
- [ ] Runtime packaging excludes source identities, labels, and fixture records.
- [ ] The generated corpus covers all named fields and representative PHI/PII variants.

## Testing

Test deterministic generation, uniqueness, seed/image correspondence, and deployment-artifact exclusion. CI must be green.

## Out of Scope

Real patient data or runtime fixture lookup.
