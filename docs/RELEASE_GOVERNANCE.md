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
