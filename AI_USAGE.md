# AI Usage

## Runtime components

| Component | Pinned selection | Purpose |
|---|---|---|
| External language model | Groq `openai/gpt-oss-120b` | Future approved prompt planning only; no patient or provider data may cross the local privacy gate. |
| Local language model | Ollama `llama3.1:8b-instruct-q4_K_M` (8B, GGUF Q4_K_M), pinned `sha256:46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e` | Runs in the local topology (`deploy/local`, TICK-059) and may see PHI, unlike the external model above. A ~7-8B quantised instruct model per LOCAL_LLM_SPEC D11, selected by TICK-062's acceptance corpus on the numbers below. Pinned by name and digest, verified on every start, because this model proposes values that reach a medical record (D6). |
| OCR | Local Tesseract (`tesseract` binary), pinned `eng` trained data | Consented, transient synthetic identity-document extraction only (name, date of birth, address); never cloud OCR. |
| Prompt contract | `ONBOARDING_CONTRACT.md` v1 | Defines deterministic onboarding fields and supportive-content text. |
| Local model prompt | `acceptance-tool-call-v1` (`ai_server/llm/prompt.py`) | The tool-call prompt the corpus below measured, and — since TICK-063 made the local model the front door for every turn — the prompt production sends. One module owns both, so the numbers below describe the running system rather than a harness that happens to agree with it. Changing it invalidates every recorded run and forces a re-measurement before CI is green. |

## How the local model was selected (TICK-062, NFR-36)

Measured, not assumed. Both candidates were run over all 44 cases of
`eval/acceptance-corpus.json` on the same host, the same runtime, the same prompt
(`acceptance-tool-call-v1`), at `temperature=0`. The corpus scores two bars separately
(LOCAL_LLM_SPEC D15): wrong writes must be zero (NFR-36), and understanding is held to a
stated 80% threshold. Full output in `evidence/TICK-062/`.

| Candidate | Wrong writes (bar: 0) | Understanding (bar: 80%) | Selected |
|---|---|---|---|
| `llama3.1:8b-instruct-q4_K_M` | **0** | **86.4%** (38/44) | yes |
| `qwen2.5:7b-instruct-q4_K_M` | 4 | 81.8% (36/44) | no |

The write bar is what decided it. `qwen2.5:7b-instruct-q4_K_M` was the provisional pin
from TICK-059 and it met the understanding threshold, but it put four wrong values into
the record across the corpus — including reading `"04/03/1985"` as 4 March rather than
3 April, and filing a phone number against a question about visit accommodations. Under
NFR-36 that is not a score to be weighed against 81.8%; it is a blocker. Quantisation
was held at Q4_K_M for both: it is what fits Apple Silicon comfortably (D5) and what
TICK-059 already pinned, so changing the model and the quantisation together would have
left neither attributable.

## Development assistance

AI-assisted development was used to draft scaffolding, tests, and documentation. All
runtime model, OCR, prompt, and privacy behavior remains subject to the project tickets
and their verification gates.
