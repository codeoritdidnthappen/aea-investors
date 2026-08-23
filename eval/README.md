# Evaluation corpora

Two files, measuring complementary halves of what reaches the model under D9.

`acceptance-corpus.json` is the realistic-phrasing corpus that decides whether the local
model is good enough to be on a path that reaches a medical record (TICK-062,
`docs/LOCAL_LLM_SPEC.md` D8/D15, PRD NFR-36).

`uncovered-turns-corpus.json` is the complement: turns that map to **no** capability at
all — distress, requests for clinical advice, medication questions, frustration and
abuse, off-topic conversation, and attempts to move the assistant out of its role
(TICK-067). It has no `expected_tool` and no `expected_write`, because for most of these
turns no approved expected output exists — establishing that was the spike. It is run by
`scripts/probe_uncovered_turns.py`, which scores nothing and records what the model
actually said; the finding is in `evidence/TICK-067/FINDING.md`. Its case format is
`id`, `category`, optional `asked`, `utterance`, optional `contract_phrase`, and `why`.

It lives here, at the top level, and deliberately **not** under `ai_server/`:
`deploy/local/ai-server.Dockerfile` does `COPY ai_server ./ai_server`, so anything under
that package ships in the deployment image. This is evaluation data and never a runtime
artifact, the same rule `scripts/evaluate_ocr_accuracy.py` states for the OCR golden set.

## Contents are synthetic (NFR-1)

Every utterance was written for these files. The names, street addresses, cities, ZIP
codes, dates of birth, email addresses and phone numbers in them are invented, and the
addresses are drawn from the same Ocean County, NJ setting the rest of the demo fixtures
use. There is no real patient information here and the files are safe to commit — which
is the point, because a corpus that could not be committed could not gate anything in CI.

That applies to `uncovered-turns-corpus.json` too, including the self-harm phrasings in
it. Several are quoted directly from the approved distress corpus in
`ONBOARDING_CONTRACT.md`, which is where they came from and why they are worded that
way; the rest were invented for the same purpose. They are in a committed file because
the alternative is discovering what the model says to them in production.

## How to run it

See `docs/RELEASE_GOVERNANCE.md` for the runbook, including which half runs in CI and
which half is manual.

```sh
# The deterministic subset. No model server: replays recorded responses.
uv run python scripts/evaluate_acceptance_corpus.py --replay eval/replays/<file>.json

# The full corpus against a live backend.
uv run python scripts/evaluate_acceptance_corpus.py \
    --backend ollama --base-url http://localhost:11434 --model qwen2.5:7b-instruct-q4_K_M

# Two backends, and where they disagree (D7).
uv run python scripts/evaluate_acceptance_corpus.py --compare \
    --backend ollama --base-url http://localhost:11434 --model qwen2.5:7b-instruct-q4_K_M \
    --other-backend vllm --other-base-url http://localhost:8000 --other-model Qwen/Qwen2.5-7B-Instruct
```

The uncovered-turn probe, which contacts the same kind of server and records rather than
scores:

```sh
# The production prompt, unmodified -- what a patient meets today under D9.
uv run python -m scripts.probe_uncovered_turns \
    --backend ollama --base-url http://localhost:11499 \
    --model llama3.1:8b-instruct-q4_K_M --variant baseline

# A recorded run, replayed. No model server.
uv run python -m scripts.probe_uncovered_turns --variant baseline \
    --replay evidence/TICK-067/transcript-baseline-llama3.1-8b-instruct-q4_K_M.json
```

## Case format

`acceptance-corpus.json`:

| Key | Meaning |
|---|---|
| `id` | Stable identifier. Referenced by evidence and by failure reports. |
| `capability` | The tool this case exercises, for grouping in the report. |
| `asked` | Optional. What the assistant asked immediately before, so "an answer to a different question" and "a refusal" are well-posed. |
| `utterance` | What the patient typed. |
| `expected_tool` | The tool a correct reading selects. This is the **understanding** bar. |
| `expected_write` | The validated values that may reach the record, or `null` for "nothing may be written". This is the **write** bar. |
| `ci` | Whether the case is in the deterministic CI subset. |
| `why` | Why the case is in the corpus. Regression phrasings name the ticket that shipped the bug. |

A field in `expected_write` may be a list, which means any one of those values is
correct. That is how genuine surface ambiguity (`"Ave"` versus `"Avenue"` when the
patient wrote one of them) is admitted **in the data, where it is reviewable**, rather
than by a fuzzy comparator in the harness that would quietly weaken a zero-wrong-writes
gate everywhere at once.

`expected_write` is compared against what `ai_server.llm.validation.validate_write`
returns — not against what the model emitted. A model that proposes nonsense the
validator refuses has not written a wrong field; it has been refused, which under NFR-36
is an acceptable outcome. That distinction is the whole reason the harness runs the real
validator instead of diffing JSON.
