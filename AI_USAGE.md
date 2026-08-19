# AI Usage

## Runtime components

| Component | Pinned selection | Purpose |
|---|---|---|
| External language model | Groq `openai/gpt-oss-120b` | Future approved prompt planning only; no patient or provider data may cross the local privacy gate. |
| OCR | Local Tesseract with pinned English trained data | Future synthetic identity-document extraction only. |
| Prompt contract | `ONBOARDING_CONTRACT.md` v1 | Defines deterministic onboarding fields and supportive-content text. |

## Development assistance

AI-assisted development was used to draft scaffolding, tests, and documentation. All
runtime model, OCR, prompt, and privacy behavior remains subject to the project tickets
and their verification gates.
