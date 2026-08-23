# AI Usage

## Runtime components

| Component | Pinned selection | Purpose |
|---|---|---|
| External language model | Groq `openai/gpt-oss-120b` | Future approved prompt planning only; no patient or provider data may cross the local privacy gate. |
| Local language model | Ollama `qwen2.5:7b-instruct-q4_K_M` (7B, GGUF Q4_K_M), pinned `sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e` | Runs in the local topology (`deploy/local`, TICK-059) and may see PHI, unlike the external model above. A ~7-8B quantised instruct model per LOCAL_LLM_SPEC D11; provisional until TICK-062's benchmark selects the final one. Pinned by name and digest, verified on every start, because this model proposes values that reach a medical record (D6). |
| OCR | Local Tesseract (`tesseract` binary), pinned `eng` trained data | Consented, transient synthetic identity-document extraction only (name, date of birth, address); never cloud OCR. |
| Prompt contract | `ONBOARDING_CONTRACT.md` v1 | Defines deterministic onboarding fields and supportive-content text. |

## Development assistance

AI-assisted development was used to draft scaffolding, tests, and documentation. All
runtime model, OCR, prompt, and privacy behavior remains subject to the project tickets
and their verification gates.
