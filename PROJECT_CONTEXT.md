# PROJECT CONTEXT — Intake

Living state: what was decided, why, what remains open, and where work stands.

**Last updated:** 2026-08-18
**Phase:** architecture updated. No application code written.

---

## 1. Decisions

| # | Decision | Rationale | Status |
|---|---|---|---|
| 1 | Build the guided-onboarding assistant, not the separate scheduling-portal brief | Preserves the selected project's chat, OCR, scheduling, and supportive-content center of gravity | Locked |
| 2 | Use behavioral-health intake as the demo domain | Preserves the previously selected sensitive-service context | Locked |
| 3 | Use the current stable OpenEMR release as the host frontend, identity provider, and EHR | The goal is to demonstrate modern AI inside an older, clunky system | Locked |
| 4 | Pin the exact stable OpenEMR version when implementation starts | Avoids unversioned deployment drift | Locked |
| 5 | Require OpenEMR login before chat access | The chat is available only to authenticated users | Locked |
| 6 | Serve the separate chat application inside an OpenEMR iframe | Keeps the user inside OpenEMR without placing AI code in the EHR process | Locked |
| 7 | The iframe talks only to the AI server | Keeps OpenEMR credentials and API authorization out of browser JavaScript | Locked |
| 8 | Use OAuth/SMART authorization-code launch with a direct AI-server callback | Gives the AI server user-delegated, scoped OpenEMR authorization | Locked |
| 9 | Store delegated OpenEMR tokens server-side; give the iframe only an AI-session cookie | Reduces bearer-token exposure | Locked |
| 10 | Use existing OpenEMR endpoints for every EHR read and write | OpenEMR remains authoritative; direct MariaDB access and a parallel scheduler are forbidden | Locked |
| 11 | Limit OpenEMR access to appointment-related data | Required data is active appointments, availability, office hours, and holiday closures | Locked |
| 12 | Support viewing, booking, rescheduling, and cancelling appointments | Full user appointment management is part of the clarified scope | Locked |
| 13 | Cancel by status and never delete | Preserves appointment history | Locked |
| 14 | Hide cancelled appointments from the patient chat; retain them for provider, office, and admin roles | Gives the user a clean view without destroying operational history | Locked |
| 15 | Use Python, FastAPI, and LangGraph for the AI server | Replaces the previous Rails choice and makes the orchestration graph explicit | Locked |
| 16 | Stream response chunks to the iframe | Improves perceived responsiveness | Locked |
| 17 | Permit an external LLM only behind a hard outbound privacy gate | Patient and provider information must never leave the AI server | Locked |
| 18 | Reject PHI/PII prompts locally rather than scrub and forward them | The user must remove the sensitive content before retrying | Locked |
| 19 | Send only the approved scheduling payload to the external LLM | Minimizes outbound context and replaces OpenEMR identifiers with anonymous slot tokens | Locked |
| 20 | On AI or LLM failure, direct the user to native OpenEMR scheduling | Avoids building a second scheduler | Locked |
| 21 | Use synthetic patient, provider, appointment, and document data only | The deployment is a demo, not a real clinical system | Locked |
| 22 | Host on two Oracle Cloud Always Free VMs | Keeps OpenEMR and AI compute separate at zero hosting cost | Locked |
| 23 | Use one reserved OCI public IP, two sslip.io hostnames, and Let's Encrypt | Provides stable HTTPS and OAuth callback names without purchasing a domain | Locked |
| 24 | Do not use Cloudflare | Oracle Cloud supplies the infrastructure | Locked |
| 25 | Keep local OCR and confirmation-before-save behavior | Preserves the selected onboarding brief's document requirement | Locked |
| 26 | Model output cannot assert an appointment write until OpenEMR confirms it | Prevents hallucinated scheduling commitments | Locked |
| 27 | Persist confirmed demographics and structured assessments in OpenEMR through existing APIs | OpenEMR remains the sole durable patient-record store | Locked |
| 28 | Use Groq Free with pinned model ID `openai/gpt-oss-120b` and Zero Data Retention | Meets the free-service constraint while retaining the hard local gate | Locked |
| 29 | Use pinned local Presidio Analyzer with built-in and custom healthcare recognizers | Reject-on-match checks stay on the Oracle AI VM | Locked |
| 30 | Use pinned local Tesseract and English trained data as the sole v1 OCR engine | Controlled synthetic IDs make the free local engine appropriate; 90% golden-set accuracy is a release gate | Locked |
| 31 | Persist session plumbing in SQLite WAL with AES-256-GCM-encrypted OAuth tokens | Active sessions survive restart without adding a patient-record store | Locked |
| 32 | Use Caddy for public ingress and automatic Let's Encrypt management | One reserved IP and sslip.io hostnames provide free HTTPS routing | Locked |
| 33 | Target current stable desktop and Android Chrome, prioritizing desktop | Keeps v1 verification within one browser family | Locked |

### Superseded decisions

The earlier Rails API, Next.js frontend, Supabase database/auth, Render/Railway,
Vercel, and AI-owned scheduling tables are superseded by decisions 3–15 and 22–24.

---

## 2. Open questions

| # | Question | Blocks |
|---|---|---|
| O-3 | Which existing endpoints in the pinned OpenEMR version cover availability, office hours, holiday closures, rescheduling, cancellation, logged-in-patient demographics, and assessment persistence? | OpenEMR adapter |
| O-10 | Which supported extension hook embeds the iframe in the patient portal on the pinned OpenEMR release? | OpenEMR portal integration |
| O-11 | Which native OpenEMR form, document, or other patient-record resource represents the structured assessment? | OpenEMR endpoint spike |
| O-12 | Which functional, visual, accessibility, or performance differences are acceptable on Android Chrome? | Mobile acceptance criteria |

Endpoint verification O-3 must happen before scheduling implementation. Missing API
coverage may not be bypassed with direct database access.

---

## 3. Proposed repository shape

This layout is proposed, not yet locked:

    openemr-module/    Minimal module and iframe launch wrapper
    ai-server/         FastAPI, LangGraph, chat UI, adapters, and privacy gate
    eval/
      ocr/             Synthetic identity documents and labels
      intents/         Synthetic chat and scheduling utterances
      privacy/         Seeded PHI/PII prompts and expected reject/allow labels
    load/              k6 scripts
    deploy/            OCI and local Docker configuration

OpenEMR itself remains an upstream dependency and is not copied into this repository.

---

## 4. Conventions

- Secrets and OAuth client credentials come from environment or deployment secret
  storage and are never committed.
- Prompt content, patient/provider information, document content, and delegated tokens
  never enter logs, traces, or analytics.
- Every external LLM request is schema-checked and capturable in tests so the privacy
  boundary can be proven with seeded sensitive values.
- Appointment facts come from OpenEMR API responses.
- No application code is written until the OpenEMR endpoint spike resolves O-3.
- Groq Zero Data Retention must be verified before model traffic is enabled; it does not
  replace the local outbound PHI/PII gate.
- Presidio rejects on any built-in or custom recognizer match; fixture values remain
  evaluation-only and are unavailable to the deployed OCR adapter.
- Tesseract is the sole v1 OCR engine; failure to meet the 90% golden-set target blocks
  deployment and reopens the decision.
- SQLite stores only hashed session handles, non-patient workflow cursors, expiry, and
  AES-256-GCM-encrypted OAuth tokens; patient drafts remain in OpenEMR.
- Git workflow follows GIT_WORKFLOW_COIDH.md.

---

## 5. Status

The interview is paused after recording the runtime services, session persistence,
ingress, and browser target. Next work should resolve the remaining OpenEMR integration
questions, beginning with endpoint coverage, before scaffolding either integration.
