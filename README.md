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

The AI server uses LangGraph for orchestration, streams response chunks to the iframe,
and performs scheduling operations through existing OpenEMR REST or FHIR endpoints.
It never connects directly to the OpenEMR database or maintains a parallel scheduler.

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
| AI server | Python, FastAPI, LangGraph |
| EHR integration | OpenEMR OAuth/SMART launch and existing OpenEMR APIs |
| Runtime AI | Groq Free with pinned model ID `openai/gpt-oss-120b` and Zero Data Retention enabled |
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

If the AI server or external LLM is unavailable, the iframe displays an unavailable
message and step-by-step instructions for opening OpenEMR's native scheduling UI. The
demo does not build a second non-AI scheduling interface.

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

`GET /health` returns a non-sensitive liveness response. `GET /` serves the embedded
chat page and `POST /api/chat` streams a turn's reply to it, gated on the AI-session
cookie. The portal module is added by its dedicated ticket.

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
