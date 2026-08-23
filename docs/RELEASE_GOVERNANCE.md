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

## Acceptance corpus gate (local model)

`eval/acceptance-corpus.json` is the realistic-phrasing corpus that decides whether the
local model may be on a path that reaches a medical record (TICK-062, LOCAL_LLM_SPEC
D8/D15). It is scored on **two bars that are never combined**:

- **Writes — zero wrong, across the whole corpus.** A field reaching the record is
  correct or refused (NFR-36). `WRITE_BAR_FAILED` blocks release, and every wrong write
  is printed individually with its input, the expected value, and the value produced.
- **Understanding — a threshold, not zero.** Misreading a request costs a retry, not a
  record. `UNDERSTANDING_BAR_BELOW_THRESHOLD` is a quality problem, reported separately
  and never traded off against the first.

### Which half runs where

| | Runs | Where | Catches |
|---|---|---|---|
| **Deterministic subset** | Every push, in CI | Replayed from a recorded run; no model server | Corpus, scoring, or validator regressions; a prompt edit riding on a stale measurement |
| **Full corpus** | Manually, before changing the model, the prompt, or the runtime | A live backend | What the model actually does now |

The subset is replayed rather than re-run because CI has no GPU and a 7B model on a CI
runner's CPU would take longer than the rest of the pipeline combined. A replay cannot
re-measure the model — the responses in it are frozen — so it is not pretending to. What
it does is refuse to run at all once `ACCEPTANCE_PROMPT_VERSION` has moved, which forces
a fresh live run before CI can be green again. That is how AC7's "a prompt change cannot
regress the write bar unnoticed" is actually enforced: not by detecting the regression,
but by making it impossible to skip the measurement.

```sh
# The CI subset, replayed. This is what runs on every push.
uv run python -m scripts.evaluate_acceptance_corpus --ci-subset \
    --replay eval/replays/ollama-qwen2.5-7b-instruct-q4_K_M.json

# The full corpus against a live backend, and re-record it.
uv run python -m scripts.evaluate_acceptance_corpus \
    --backend ollama --base-url http://localhost:11434 \
    --model qwen2.5:7b-instruct-q4_K_M \
    --record eval/replays/ollama-qwen2.5-7b-instruct-q4_K_M.json

# Two runtimes, same corpus, and where they disagree (D7).
uv run python -m scripts.evaluate_acceptance_corpus --compare \
    --backend ollama --base-url http://localhost:11434 --model qwen2.5:7b-instruct-q4_K_M \
    --other-backend vllm --other-base-url http://localhost:8000 \
    --other-model Qwen/Qwen2.5-7B-Instruct
```

Run it as `python -m scripts.…` rather than `python scripts/….py`: the project is not
installed into the environment, so executing the file by path puts `scripts/` on
`sys.path` instead of the repository root and `import ai_server` fails.

`BACKENDS_DIVERGE` is an expected output, not a failure. D7 serves GGUF Q4 locally and
fp16 or AWQ in the deployment, so the same model name is not the same numerics; the
divergence is measured and listed per case rather than averaged into a single number.
Both backends must independently meet the write bar.
