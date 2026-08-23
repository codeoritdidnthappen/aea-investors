# TICK-062 — the acceptance corpus selected the local model, on measured results

**Date:** 2026-08-23
**Host:** Apple Silicon (arm64), Docker Desktop 29.6.2, 8.3 GB VM, 18 CPU, no GPU
passthrough — every model below ran on CPU.
**Reproduce:** `sh evidence/TICK-062/run_live_verification.sh`

The model server used for these runs is a standalone `tick062-ollama` container on port
11499 with its own volume, deliberately separate from `deploy/local` so an eval run
neither disturbs a running stack nor inherits its state.

## What is pinned

| | Value | Confirmed by |
|---|---|---|
| Runtime | `ollama/ollama:0.32.15` | image tag, never `latest` |
| Selected model | `llama3.1:8b-instruct-q4_K_M` | `sha256sum` of the registry manifest |
| Digest | `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e` | `ollama list` ID column shows its first 12 chars, `46e0c10c039e` |
| Prompt | `acceptance-tool-call-v1` | printed in every report header |
| Corpus | `eval/acceptance-corpus.json`, 44 cases, 39 in the CI subset | `--ci-subset` |

## Results

| # | Claim | Result |
|---|---|---|
| 1 | The harness runs the corpus against a real model server | **pass** — 44 cases, 3 models, 4 full runs |
| 2 | It reports the two bars separately, never blended | **pass** — see any run file; the write bar is a count, understanding a percentage |
| 3 | A wrong write is reported individually with input, expected and produced | **pass** — `run-qwen2.5-7b-instruct-q4_K_M.txt` |
| 4 | `llama3.1:8b-instruct-q4_K_M` meets the write bar | **pass** — 0 wrong writes / 44 |
| 5 | `qwen2.5:7b-instruct-q4_K_M` (the provisional pin) does not | **fail, as measured** — 4 wrong writes / 44 |
| 6 | Two runtimes over one corpus, divergence reported per case | **pass** — 41/44 agreement, 3 divergences |
| 7 | The CI subset runs with no model server | **pass** — `run-ci-subset-replay.txt`, exit 0 |

### The selection

| Candidate | Wrong writes (bar: 0) | Understanding (bar: 80%) |
|---|---|---|
| `llama3.1:8b-instruct-q4_K_M` | **0** | **86.4%** (38/44) |
| `qwen2.5:7b-instruct-q4_K_M` | 4 | 81.8% (36/44) |

Both cleared the understanding threshold and the gap between them there — 86.4% against
81.8% — is well inside what four turns of noise could produce. The write bar is what
decided it, which is D15 working exactly as designed: the two bars did not have to be
weighed against each other, because one candidate corrupted records and the other did
not.

The four qwen wrong writes, in full in `run-qwen2.5-7b-instruct-q4_K_M.txt`:

```
  WRONG WRITE [demographics-dob-numeric-ambiguous] via update_demographics
    input:    'My date of birth is 04/03/1985.'
    field:    date_of_birth
      expected: '1985-04-03'
      produced: '1985-03-04'
```

A date of birth into a chart, off by a month, with nothing to notice it. Also: a phone
number filed against a question about visit accommodations; an apartment number folded
into `street1` and dropped from `street2`; and `"No, nothing like that."` recorded as
`help_type: not_sure_yet` when the question asked was about accommodations.

### Backend divergence (D7)

`llama3.1:8b-instruct-q4_K_M` against `llama3.1:8b-instruct-q5_K_M` — same weights, same
prompt, same host, same `temperature=0`, different quantisation. **41/44 agreement**, and
the three disagreements are not cosmetic:

| Case | Q4_K_M | Q5_K_M |
|---|---|---|
| `address-correction-mid-sentence` | correct write | **wrong write** — took `30 Pine Road`, the value the patient corrected away from |
| `cancel-then-rebook` | refused | **wrong write** — invented a whole address |
| `assessment-help-type-unsure` | correct write | no write (replied instead) |

The `cancel-then-rebook` divergence is the one worth reading twice. Asked *"Can you move
my Thursday appointment to the week after?"*, Q5_K_M emitted an `update_address` call
containing `12 Oak Street, Anytown, CA 12345` — an address that appears nowhere in the
conversation, for a request that was about an appointment. It passed field validation
because every component is individually well-formed. Only the corpus caught it.

**Q5_K_M is the higher-precision quantisation and it scored worse: 2 wrong writes to
Q4_K_M's 0.** That is the D7 risk stated as a measurement rather than a caveat — "same
model name, different numerics, different outputs", including different in the direction
nobody would have predicted. It is also the argument for pinning the quantisation by
digest rather than trusting the model name, which `deploy/local/ollama-entrypoint.sh`
already does.

### The validator is doing real work

Across the four runs the harness recorded 3–4 **refused** writes per run: values the
model proposed that TICK-061's validators rejected before they could reach a record.
Those are counted separately from wrong writes and are not failures — under NFR-36 a
refusal is the correct outcome when a value cannot be produced correctly. The clearest
example is `book-slot-never-offered`, where the model reached for a slot token it had
not been given and `validate_offered_slot` refused it, exactly as TICK-059's evidence
predicted it would need to.

This is why the harness scores `validate_write` output rather than diffing the model's
JSON: without that, every one of those refusals would have been miscounted as a wrong
write, and the write bar would have been unreachable by any model.

## The guards discriminate

Each of these was run to confirm the harness fails when it should, not just passes when
it should (`ai_server/tests/test_evaluate_acceptance_corpus.py`, 39 tests):

| Seeded fault | Reported as |
|---|---|
| Right tool, one field altered (`Bridge Avenue` → `Bridge Road`) | wrong write, blocker, field named with both values |
| Write emitted where nothing should be written | wrong write, every field listed as `(nothing should be written)` |
| The literal TICK-050 string as `street1` | **refused**, not wrong — the validator holds |
| Wrong tool chosen | understanding miss, write bar still met |
| Unparseable response | understanding miss, never a wrong write |
| 99 correct cases + 1 wrong write | not release ready — the write bar is a count, not a percentage |
| Replay recorded under an older prompt | refused to run at all |

## Not verified here

- **vLLM.** Nothing on this page was measured against vLLM. It does not run on this host
  (arm64, no CUDA) and no vLLM adapter exists in the repo — `LLM_PROVIDERS` is
  `("groq", "ollama")`, and TICK-066 owns adding one and depends on this ticket. The
  harness takes an arbitrary OpenAI-compatible base URL, which is the shape vLLM serves,
  so the `--compare` path is ready for it; what was substituted is a quantisation
  comparison within Ollama, which exercises the same divergence mechanism on the runtime
  that exists. **The Ollama-versus-vLLM half of AC5 remains open.**
- **Constrained decoding against the full tool surface.** Ollama 0.32.15 rejects
  `tool_call_json_schema()` with `400 failed to parse grammar` — the discriminated union
  over ten argument models is more than its grammar compiler takes. Generation is
  constrained to the envelope instead (one object, two keys, a published tool name) and
  `parse_tool_call()` validates everything else, which is the path TICK-060 wrote for
  runtimes that do not constrain generation. Whether vLLM's outlines backend accepts the
  full schema is untested. `--response-format tool_call` exists to find out.
- **Latency and concurrency.** Every run was CPU-only in an 8 GB Docker VM; wall clock
  here predicts nothing about a deployed GPU box or about continuous batching.
- **Prompt optimality.** `acceptance-tool-call-v1` was iterated only as far as selecting
  a model required — two rounds, both driven by wrong writes the corpus surfaced. It is
  not a tuned prompt and the remaining 6 misread turns are where tuning would start.
- **Restatement fidelity (D14).** `general-knowledge-carrying-patient-context` scores
  routing only. Whether the restatement carries the patient's context out is not
  measured here.

## A pre-existing bug noticed, not fixed

`scripts/evaluate_ocr_accuracy.py` cannot be run the way `docs/RELEASE_GOVERNANCE.md`
documents it. `uv run python scripts/evaluate_ocr_accuracy.py` fails with
`ModuleNotFoundError: No module named 'ai_server'`: the project has no `[build-system]`
and so is never installed into the environment, and executing a file by path puts
`scripts/` on `sys.path` rather than the repository root. It is not in CI, so nothing
caught it. Out of scope for this ticket and left alone; the new harness sidesteps it by
being documented and run as `python -m scripts.evaluate_acceptance_corpus`.
