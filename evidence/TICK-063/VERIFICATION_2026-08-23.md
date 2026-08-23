# TICK-063 — the local model is the front door, verified on real turns

**Date:** 2026-08-23
**Host:** Apple Silicon (arm64), Docker Desktop 29.6.2, no GPU passthrough — the model
ran on CPU.
**Reproduce:** `sh evidence/TICK-063/run_live_verification.sh`

The unit and integration suites (`ai_server/tests/test_model_turn.py`, 63 tests) mock the
model at the wire, so what they prove is that *this codebase* executes a tool call
correctly. They cannot prove the half that was genuinely uncertain: whether the pinned
model, reading the shipped prompt, routes a real conversation. That is what this run is
for, and it is what `run-live-turns.txt` records verbatim.

The model server is a standalone container on port 11499 with its own volume,
deliberately separate from `deploy/local` so a verification run neither disturbs a
running stack nor inherits its state.

## What is pinned

| | Value | Confirmed by |
|---|---|---|
| Runtime | `ollama/ollama:0.32.15` | image tag, never `latest` |
| Model | `llama3.1:8b-instruct-q4_K_M` (D17) | `ollama list` ID `46e0c10c039e` |
| Digest | `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e` | matches `deploy/local/docker-compose.yml:144` |
| Prompt | `acceptance-tool-call-v1` | printed in the run header; `ai_server/llm/prompt.py` |
| Grammar | the TICK-060 envelope (`tools.envelope_json_schema`) | Ollama 0.32.15 cannot compile the full union — see TICK-062 |

The prompt moved from `scripts/evaluate_acceptance_corpus.py` into `ai_server/llm/prompt.py`
in this ticket, **byte-for-byte unchanged**, because the runtime now sends it and
`deploy/local/ai-server.Dockerfile` copies `ai_server/` and nothing else. The harness
imports it from there, so the prompt the corpus scored and the prompt production sends
are one object. Every recorded replay stays valid and the version is unbumped.

## Results

Every row below is a real turn against the real model; the transcript is
`run-live-turns.txt`.

| # | Claim | Result |
|---|---|---|
| 1 | Every turn reaches the model; no phrasing is matched first (AC1) | **pass** — "update my address" / "start onboarding" reach the model like anything else; `onboarding_mode`/`address_update_mode` are called by no code path |
| 2 | A proposed write is read back and nothing is saved (AC2) | **pass** — every write turn was `awaiting_confirmation` first, with no OpenEMR request |
| 3 | A confirmed write reaches OpenEMR with the *validator's* values (AC2) | **pass** — `PUT .../demographics {"street":"88 Larch Street",...,"state":"NJ",...}` |
| 4 | The reply streams; the pre-stream pause is measured (AC3) | **pass** — 16 turns, min 0.61s, median 1.78s, max 3.14s, reported per turn |
| 5 | A pending confirmation survives to the next turn (AC4) | **pass** — three separate conversations confirmed a change proposed a turn earlier |
| 6 | An onboarding position survives as a draft id and is resumed (AC4) | **pass** — one `POST .../assessment`, then `PUT .../assessment/draft-live` |
| 7 | "actually, make it 2004" mid-confirmation does the right thing (AC5) | **pass** — 2003-04-01 read back, corrected to 2004-04-01, read back again, nothing written until agreed; the write was `{"DOB":"2004-04-01"}` |
| 8 | Changing the subject abandons the pending change (AC5) | **pass** — "actually never mind, what appointments do I have?" listed appointments and wrote no surname |
| 9 | Nothing reaches Groq on any path (AC6) | **pass** — every host contacted: `localhost` (the model) and `openemr.local`. Requests to Groq: none |
| 10 | Booking, cancelling, listing, onboarding answers, address and demographics updates still work (AC7) | **pass** — see rows 3, 6, 8 and the `find_slots` → choose → confirm → `POST .../appointment` sequence |
| 11 | A value the validator will not vouch for is refused, never written (D15/NFR-36) | **pass** — "counselling, and psychiatric help too" was refused with the choices named back; nothing was written |
| 12 | Not one wrong value reached the record across the run | **pass** — every OpenEMR body in the transcript is what the patient actually said |

### The pre-stream pause (D16)

| | Seconds |
|---|---|
| min | 0.61 |
| median | 1.78 |
| max | 3.14 |

Recorded, not hidden: `ModelTurnService` reports `TurnMetrics.routing_seconds` for every
turn and `log_turn_metrics` writes it to the log with the tool and the outcome and
nothing else — no patient text and no model text. A first, cold turn in an earlier run of
this same script took **10.1s**; the numbers above are with the model resident. Both are
inside the chat page's 30-second stall timeout, and the deployed box is expected to be
slower than this development Mac (D5, D7).

## Rough edges, recorded rather than smoothed over

- **A partial address is refused with a generic sentence, not a follow-up question.**
  "I live on Larch Street now." produced an `update_address` call missing city, state and
  ZIP, which `parse_tool_call` refused. Prompt rule 2 asks for `reply` here and the model
  did not follow it. The outcome is safe — nothing was written, which is the bar NFR-36
  sets — but the patient gets "I could not work out how to handle that request" rather
  than "what's your city?". This is the 13.6% of the corpus llama3.1 does not understand
  (TICK-062: 86.4%), showing up as designed: a misunderstood request costs a retry, not a
  record. Prompt work beyond `acceptance-tool-call-v1` is left open in
  `docs/LOCAL_LLM_SPEC.md`.
- **"Both, please — counselling and medication support." was refused on the first try**
  and recorded correctly on the second. The model proposed an answer outside the closed
  set; `validate_assessment_answer` refused it and named the choices back, and the
  patient's next message landed on `both`. Again: a retry, not a wrong record.

## Not verified here

- **The containerised deployment.** `deploy/local` still defaults to
  `LLM_PROVIDER=groq`, and the ai-server image is not bind-mounted, so exercising the
  model path through the browser needs `docker compose up --build` with
  `LLM_PROVIDER=ollama`. That rebuild was not performed from this worktree: the running
  stack is shared, and rebuilding it would have disturbed work in progress on sibling
  tickets. What ran here is the same `ModelTurnService`, the same prompt, the same
  grammar and the same model, in-process.
- **The OpenEMR wire.** Mocked in this run, and recorded above only as the request body
  produced. That format is already proven against a real OpenEMR 8.3.0 in
  `evidence/TICK-049/ADDRESS_WRITE_EVIDENCE.md` (demographics and address columns) and
  `evidence/TICK-031` (appointments); nothing in this ticket changes it. The one new
  body shape — a partial identity write, `{"DOB": ...}` alone — uses the same
  `PUT /portal/patient/demographics` route and the same
  `BaseService::buildUpdateColumns` behaviour TICK-049 verified for the address-only body.
- **`ask_general_knowledge`.** Answered honestly as unavailable rather than wired to
  Groq. TICK-064 owns the D14 restatement path; until it exists there is no egress to
  verify, which is why row 9 is a stronger claim than it looks.
- **vLLM.** Unchanged from TICK-062: no vLLM adapter exists and vLLM does not run on this
  host. TICK-066 owns it.
- **The deterministic handlers.** `onboarding_chat.py` and `address_chat.py` are still in
  the tree and still built at startup; they are simply reached by no request. TICK-065
  deletes them, which is why this ticket leaves them alone.
