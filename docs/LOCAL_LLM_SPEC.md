# Local LLM — architecture spec

**Status:** in progress. Decisions below are settled; the interview continues.
**Started:** 2026-08-23

## Why

Keeping PHI away from an external model currently forces every way a patient might
phrase anything to be hand-coded. That set is unbounded. The address field alone took
five tickets and still wrote `"Update it to: 2002 Bridge Avenue"` into a chart, because
`_parse_freeform_address` assigns the first comma-separated segment to `street1`
unexamined. Every new field restarts that work; every unanticipated phrasing is a new
bug.

A local model may see PHI. Intent handling stops being a parsing problem and becomes a
prompting-and-validation problem — bounded work that generalises to the next field
instead of restarting.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Adopt a local LLM; stop extending the deterministic parsers | The parsing approach never converges. Its cost is not the next fix but the infinite series of them |
| D2 | Local model for anything PHI-bearing or otherwise sensitive; Groq for non-sensitive work | Keeps an external model available where it adds value without it ever being the thing that sees patient data |
| D3 | **The boundary is structural, not classificatory.** Groq never receives text the patient typed. It receives only payloads this codebase constructs from structured, de-identified data (anonymous slot tokens, office hours, rules) | Today `chat.py:427` forwards the raw user message and relies on Presidio to catch PHI — a detector that fails open on anything it does not recognise. You cannot leak what you never put in the payload |
| D4 | Presidio still scans every constructed payload before egress | Defence in depth. Two independent controls: structural (PHI is never placed in the payload) and detective (Presidio catches it if it somehow was). Strictly stronger than today, where Presidio is the only control |
| D5 | Apple Silicon for development; Oracle Cloud for deployment later | Recorded as a known risk: a model comfortable in unified memory on a Mac behaves differently on OCI ARM cores. See Risks |
| D6 | **Model proposes, code disposes.** The model emits a structured tool call; this codebase validates every field independently; the patient confirms before any write | The model is probabilistic and now produces values that reach a medical record. The validator is backend-agnostic and holds even when the model is wrong, the prompt regresses, or the runtime changes |
| D7 | Ollama locally, vLLM deployed, selected by config from day one | `LLM_PROVIDER` already exists in `.env.example` and is read only by `health.py` — the config surface exists, the dispatch does not. `GroqClient` is a two-method Protocol, so a second adapter is contained |
| D8 | An eval set is a first-class deliverable, not a test afterthought | Two runtimes means two behaviours. Realistic patient phrasings with expected structured output, run against whichever backend is configured, is the only way to know they agree — and the only way "does this handle address updates" is answerable before production |
| D9 | The model owns all turn routing, including deciding when scheduling work is needed | The cleanest end state, with no leftover routing heuristics to maintain |
| D10 | The model's routing choice never bypasses D3 or D4 | The model may *decide* scheduling help is needed. It does not assemble the outbound payload: this codebase builds it from structured data, Presidio scans it, then it leaves. The model's judgement selects an action, it does not gate egress |

| D11 | A ~7–8B quantised instruct model (Llama 3.1 8B / Qwen2.5 7B / Mistral 7B class) | Comfortable on Apple Silicon, strong tool-calling in current instruct tunes, and small enough that a modest GPU serves it well or an ARM CPU serves it slowly but not absurdly. At this size complex multi-turn judgement is uneven, so the prompts and D6's validator carry more of the load |

| D12 | Delete `address_chat.py` and `onboarding_chat.py`. When the model server is unavailable the chat reports unavailable | One paradigm, one code path, no drift. A retained deterministic fallback would rot from disuse and then produce exactly the bug class being eliminated — a bad parse reaching a chart — at the worst possible moment. An honest outage is preferable, and the portal itself keeps working |

| D13 | Groq is narrowed to general-knowledge questions carrying no patient context. The local model owns everything patient-specific, **including scheduling planning** | Scheduling moves from Groq to the local model, so `PlanningOutput` / `SchedulingContext` / `SchedulingRules` stop being an outbound concern. An external model stays available where it is clearly safe, without being on any path that touches the record |

| D14 | Resolves D3 vs D13. The local model **restates** a general-knowledge question in canonical, context-free form as a schema'd tool-call field. That restatement is what leaves, after Presidio scans it. The patient's own words never egress | Keeps the boundary structural: outbound text is always something this system generated, never something the patient typed. A restatement could in principle carry something over, which is exactly what D4's Presidio scan is positioned to catch |

| D15 | Two separate eval bars. **Any field reaching the record is right or refused — zero wrong writes, measured across the whole corpus.** Understanding the patient may be imperfect: a misunderstood request is a bad turn, not a bad record | Prices the failures by their actual harm. A misunderstood question costs a retry; a wrong street silently written into a chart is the thing this project already did once. D6's validate-then-confirm is what makes the strict half achievable rather than aspirational |

| D16 | Stream tokens as they generate; accept a visible pause before the first token | The routing inference must finish before anything can stream, so the pause is real and worth being honest about. Streaming is how the chat already behaves (`stream()` is on the client Protocol), needs no new machinery, and degrades gracefully when the deployed box is slower than the development Mac |

## Tool surface (draft — for review)

The model never acts directly. It emits one tool call under a strict schema; this
codebase validates every field (D6) and executes. Drawn from the services that already
exist, so this is a re-facing of current capability rather than new function:

| Tool | Backed by | Writes? |
|---|---|---|
| `update_address` | `PatientDemographicsUpdateService` | yes — validate + confirm |
| `update_demographics` | `PatientDemographicsUpdateService` | yes — validate + confirm |
| `record_assessment_answer` | `AssessmentDraftService` | yes — validate + confirm |
| `list_appointments` | `AppointmentDiscoveryService` | no |
| `find_slots` | `SlotDiscoveryService` | no |
| `book_appointment` | `BookingService` | yes — validate + confirm |
| `cancel_appointment` | `CancellationService` | yes — validate + confirm |
| `extract_document_fields` | `OcrService` | no — feeds a confirm step |
| `ask_general_knowledge` | Groq, via D14 restatement | no — egress, Presidio-scanned |
| `reply` | none | no — plain conversational answer |

`RescheduleService` is deliberately absent: TICK-020 established that no OpenEMR service
method exists for it.

## Risks

- **Runtime divergence (D7).** Ollama serves GGUF (typically Q4); vLLM serves fp16 or
  AWQ. Same model name, different numerics, different outputs. Concurrency differs too —
  limited parallelism versus continuous batching — so local load testing predicts little
  about deployed latency. Mitigated by D6 (backend-agnostic validation) and D8 (eval
  against both).
- **Deployment target (D5).** Current deploy configs are local-only; the OCI work exists
  only on the `archive/oci-cloud-deploy` tag. A model that runs comfortably on Apple
  Silicon may need a paid GPU shape on OCI. Unresolved.
- **Restatement fidelity (D14).** A restatement that drops or distorts the question
  yields a correct-looking answer to something the patient did not ask. This is a quality
  risk rather than a privacy one, but it is invisible to the patient, who never sees the
  restatement.
- **Vendor quirks already observed.** `_strict_schema()` exists because Groq's strict
  mode requires every property in `required`, with optionality as a nullable union. That
  is provider-specific; the adapters must not assume it generalises.
- **Hard dependency on the model server (D12).** With no fallback path, model-server
  availability becomes chat availability. The health endpoint must report it, and the
  unavailable message must be honest rather than looking like a broken feature.
- **The model makes the routing judgement (D9).** Accepted deliberately, and bounded by
  D10: a wrong routing decision selects the wrong action, it cannot place patient data
  into an outbound payload.

| D17 | **`llama3.1:8b-instruct-q4_K_M`**, pinned by digest. Settles the model and quantisation left open under D11 | Chosen on the corpus, not on reputation. Over all 44 acceptance cases it produced **zero wrong writes** and understood 86.4%; `qwen2.5:7b-instruct-q4_K_M`, the provisional pin from TICK-059, produced **four wrong writes** at 81.8% understanding — including a date of birth off by a month and a phone number filed against a question about accommodations. D15 makes that decisive rather than a trade: the understanding scores are close enough to be noise, and one of the two corrupts records. Quantisation held at Q4_K_M for both so the model is the only variable. Numbers in `AI_USAGE.md` and `evidence/TICK-062` |

## Open

The tool surface above needs review. Prompt design beyond the tool-call prompt the
corpus measures (`acceptance-tool-call-v1`).

**Confirmed by measurement, not left as an assumption:**

- Ollama 0.32.15 **cannot** constrain generation with `tool_call_json_schema()` — the
  discriminated union over ten argument models fails its grammar compiler with a 400.
  TICK-060 AC1's first half is therefore unavailable on the development runtime, and
  `parse_tool_call()` carries the guarantee alone there. Generation is constrained to
  the envelope (one object, two keys, a published tool name) instead, which is what that
  runtime will compile. Whether vLLM's outlines backend accepts the full schema is
  untested — see the risk below.
- Runtime divergence (D7) is still **unmeasured against vLLM**. vLLM does not run on the
  Apple Silicon development host (arm64, no CUDA) and no vLLM adapter exists yet
  (`LLM_PROVIDERS` is `("groq", "ollama")`; TICK-066 owns it and depends on this
  ticket). The harness takes an arbitrary OpenAI-compatible endpoint, so it is ready to
  measure this the day a vLLM server exists; what has been measured instead is
  quantisation divergence within Ollama, which is the same mechanism on the runtime
  that is available. See `evidence/TICK-062`.
