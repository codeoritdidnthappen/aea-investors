# Release governance gate

Run the gate against the directory prepared for deployment:

```sh
uv run python scripts/verify_release_governance.py --artifact-root <artifact-directory>
```

It reports `AI_USAGE_VALID` when the runtime inventory contains pinned model and OCR
selections plus a versioned prompt contract. It reports `ARTIFACT_GOVERNANCE_VALID`
when the directory contains no OCR labels, fixture-source records, synthetic identity
images, or privacy golden-corpus data.

Any `GOVERNANCE_GATE_FAILED` output blocks release preparation. The output identifies
only the gate and artifact path; it never prints fixture values.

## OCR golden-set accuracy gate

Run the gate against an isolated synthetic-ID golden set generated offline by
`ai_server.fixtures.generator` (TICK-006):

```sh
uv run python scripts/evaluate_ocr_accuracy.py --golden-set <golden-set-directory>
```

It runs the pinned local Tesseract binary and English trained data over every golden-set
image, calculates field-level accuracy for name, date of birth, and address, and reports
`OCR_GOLDEN_SET_ACCURACY_VALID` only when every field reaches the 90% NFR-29 target.

Any `OCR_GOLDEN_SET_BELOW_THRESHOLD` output blocks local-demo readiness and reopens the
pinned Tesseract engine decision; it never authorizes adding another OCR engine or
lowering the target.
