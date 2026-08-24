# Intake

An AI-assisted behavioral-health onboarding demo embedded inside
[OpenEMR](https://github.com/openemr/openemr). A logged-in patient opens the chat from
OpenEMR, completes the guided intake flow, and can view, book, reschedule, or cancel
appointments through conversation.

**Status:** foundation established; feature implementation has not started.

---

## Product shape

OpenEMR is the host application, identity provider, and system of record for
appointments, demographics, structured assessments, and all other persisted patient data.
A small OpenEMR patient-portal integration displays the separately hosted chat UI in an
iframe. The iframe talks only to a FastAPI AI server; it never calls OpenEMR directly.

**How a turn is handled.** Every message the patient sends goes to a local model, and
nothing inspects it first — there is no mode detection, no keyword routing, and no
deterministic handler to fall back to. The model reads the turn plus the conversation
state the AI server recorded, and returns exactly one tool call under a strict schema.
The AI server then validates every field itself, reads the validated values back for the
patient to confirm, and only then performs the operation through existing OpenEMR REST or
FHIR endpoints. The model proposes; this codebase disposes. It never connects directly to
the OpenEMR database or maintains a parallel scheduler.

The model runs inside the deployment because it is allowed to see patient data. The one
branch that reaches an external model — a general-knowledge question — sends a canonical
restatement the local model composed, never the patient's own words.

The selected onboarding requirements remain:

- guided assessment chat;
- local image-to-text extraction with confirmation before saving;
- appointment discovery and management;
- supportive content at friction points.

The appointment scope includes active appointments, provider availability, office
hours, holiday closures, booking, rescheduling, and cancellation. Cancellation is a
status change, never deletion. Cancelled appointments remain visible to authorized
provider, office, and admin users but are hidden from the patient-facing chat.

---

## Confirmed stack

| Layer | Choice |
|---|---|
| Host UI, login, and EHR | Current stable OpenEMR release, pinned at implementation |
| Embedded UI | Chat UI inside an OpenEMR iframe |
| AI server | Python, FastAPI |
| EHR integration | OpenEMR OAuth/SMART launch and existing OpenEMR APIs |
| Runtime AI | Local `llama3.1:8b-instruct-q4_K_M`, pinned by digest, served over an OpenAI-compatible API (Ollama in development, vLLM when deployed) |
| General-knowledge answers only | Groq Free with pinned model ID `openai/gpt-oss-120b` and Zero Data Retention enabled |
| Outbound privacy gate | Local Presidio Analyzer with built-in and custom healthcare recognizers |
| OCR | Pinned local Tesseract and English trained data |
| Session store | SQLite in WAL mode; OAuth tokens encrypted with AES-256-GCM |
| Deployment | Two Oracle Cloud Always Free VMs |
| Public names | Two sslip.io hostnames on one reserved OCI public IP |
| TLS | Caddy-managed Let's Encrypt certificates |
| Browser | Current stable desktop and Android Chrome; desktop is the priority |

Cloudflare, Supabase, Rails, Next.js, Render, Railway, and Vercel are not part of the
revised deployment.

---

## Privacy boundary

The demo uses synthetic patient, provider, appointment, and identity-document data only.

Patient and provider information must never be sent to an external LLM. Before any
external request, the AI server checks the user's prompt for PHI or PII. A prompt that
fails the check is not scrubbed or forwarded; it is returned locally with instructions
to remove the sensitive content and try again.

The gate runs pinned Presidio locally with built-in and custom healthcare recognizers;
any match rejects the request. Groq Zero Data Retention must be verified before model
traffic is enabled. Tesseract is the sole v1 OCR engine and must pass the 90% synthetic-ID
golden set before deployment.

Approved outbound scheduling context is request-specific and minimal: validated prompt
text, current date/time, timezone, office hours, closure intervals, anonymous open-slot
tokens and times, scheduling rules, and a structured response schema. Real OpenEMR
identifiers and delegated access tokens stay inside the AI server.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the trust boundaries and exact payload shape.

---

## Failure behavior

**Model availability is chat availability.** The deterministic intent handlers were
deleted rather than kept as a fallback: a path nobody exercises rots, and the failure it
eventually produces — a bad parse written into a chart — is the one this design exists to
end. An honest outage is the better trade.

So when the model server is unreachable, the chat says the assistant is temporarily
unavailable and that the patient's portal still works. No degraded path runs, and in
particular **no write is attempted** — not even a change already validated and read back
and waiting on a confirmation. `GET /health` reports `model_server` alongside its other
dependencies so the outage is visible in monitoring first.

An unreachable *external* LLM is a much smaller failure: it costs general-knowledge
answers and nothing else.

The demo does not build a second non-AI scheduling interface. The next step offered to
the patient is OpenEMR's own patient-portal scheduling screen.

---

## Deployment shape

    Reserved OCI public IP
    ├── emr.<ip>.sslip.io  → OpenEMR VM + MariaDB
    └── chat.<ip>.sslip.io → reverse proxy → private AI VM

Both hostnames use HTTPS. OpenEMR sends the OAuth authorization code directly to an
AI-server callback. The AI server stores user-delegated OpenEMR tokens server-side;
the iframe receives only a secure, HttpOnly AI-session cookie.

---

## Quickstart

The local AI-server foundation uses Python and uv. From a clean checkout:

```sh
uv run --locked --group dev ruff format --check .
uv run --locked --group dev ruff check .
uv run --locked --group dev pytest
uv run uvicorn ai_server.app.main:app --host 127.0.0.1 --port 8000
```

The AI server exposes six routes:

| Route | Purpose |
|---|---|
| `GET /health` | Non-sensitive liveness and dependency reachability, including the model server |
| `GET /` | The embedded chat page |
| `POST /api/chat` | Streams a turn's reply, gated on the AI-session cookie and an `Origin` check |
| `POST /api/logout` | Ends the AI session and clears its cookie; the portal calls this on sign-out |
| `GET /oauth/launch` | The portal panel's entry point; skips the round trip when a session is already live |
| `GET /oauth/callback` | Exchanges the code and resolves where the patient lands |

Where an authorization lands the patient is a fixed invariant, not a setting: top-level
completion goes to the portal dashboard, in-panel completion loads the chat in the panel.
See [ADR-8](ARCHITECTURE.md#adr-8--the-chat-is-a-panel-never-a-landing-page).

The OpenEMR portal module lives in
[openemr_modules/aeai-portal-chat](openemr_modules/aeai-portal-chat); it adds the
dashboard tile and the chat panel.

The full local demo topology — pinned OpenEMR, MariaDB, and the AI server as
separate Docker Compose services with persistent local state — lives in
[deploy/local](deploy/local/README.md).

---

## Documents

| File | Contents |
|---|---|
| [PRD.md](PRD.md) | Testable product and privacy requirements |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, trust boundaries, OAuth flow, and OCI deployment |
| [ONBOARDING_CONTRACT.md](ONBOARDING_CONTRACT.md) | Minimal v1 field, draft, completion, and supportive-content contract |
| [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) | Durable decision log and open questions |
| [CHANGES.log](CHANGES.log) | Chronological planning history |
| [GIT_WORKFLOW_COIDH.md](GIT_WORKFLOW_COIDH.md) | Branching, commits, pull requests, and the merge gate |
| [AI_USAGE.md](AI_USAGE.md) | Pinned runtime models, OCR engine, and prompt-contract version |
| [tickets/BACKLOG.md](tickets/BACKLOG.md) | Epics, execution order, and requirement traceability |
