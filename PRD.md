# PRD — Intake

**Product:** AI-assisted behavioral-health onboarding in OpenEMR
**Status:** architecture updated, pre-implementation
**Last updated:** 2026-08-18

---

## 1. Overview

Intake turns a long, stressful behavioral-health registration form into a guided
conversation. The selected project still includes conversational assessment, local
document extraction, appointment scheduling, and supportive content.

The demo will now run inside the current stable
[OpenEMR](https://github.com/openemr/openemr) patient portal. A patient must log in to OpenEMR
before opening the embedded chat. OpenEMR remains the identity provider and source of
truth for appointments, demographics, and structured assessments; a separate AI server
orchestrates the conversation and uses existing OpenEMR APIs for all patient-record reads
and writes.

Only synthetic data will be used. Groq's `openai/gpt-oss-120b` is the hosted external
LLM, with Zero Data Retention enabled; patient and provider information may never cross
that boundary.

---

## 2. Goals

- Demonstrate a streamed AI chat inside an older OpenEMR interface.
- Preserve the guided onboarding, OCR confirmation, scheduling, and supportive-content
  requirements of the selected project.
- Let a logged-in user inspect and manage appointments conversationally.
- Keep OpenEMR authoritative for appointment state and authorization.
- Prove an outbound privacy gate that blocks PHI/PII before external LLM calls.
- Deploy the complete demo on Oracle Cloud free resources.

## 3. Non-goals

- A staff-, provider-, office-, or admin-facing AI chat.
- A replacement for OpenEMR's native scheduling interface.
- Direct database access from the AI server.
- A parallel appointment database or scheduling engine.
- Physical deletion of cancelled appointments.
- Real patient, provider, or appointment data.
- A production HIPAA-compliance claim.
- Cloudflare or paid hosting.

---

## 4. Functional requirements

### OpenEMR host and identity

- **FR-1 [must]** An unauthenticated user cannot open or use the chat.
- **FR-2 [must]** A logged-in user can open the chat inside the OpenEMR frontend without
  navigating to a separate application.
- **FR-3 [must]** OpenEMR launches the AI application through OAuth/SMART authorization
  code flow, and the AI server associates the resulting delegated authorization with
  an opaque AI session.
- **FR-4 [must]** The iframe communicates only with the AI server and never calls an
  OpenEMR endpoint directly.
- **FR-31 [must]** The chat is a panel inside the portal, never a landing page. An
  authorization that completes at top level lands the patient on the portal dashboard;
  one that completes inside the chat panel loads the chat in that panel. The patient is
  never left on the standalone chat page, and is never returned to the page they were on
  when their session ended. No `next=`, `redirect=`, or equivalent parameter may
  influence either destination. Fixed invariant:
  see [ADR-8](ARCHITECTURE.md#adr-8--the-chat-is-a-panel-never-a-landing-page).
- **FR-32 [must]** The chat begins no authorization until the patient opens it. Rendering
  the portal dashboard must not start an OAuth flow, navigate the patient anywhere, or
  prompt for credentials on behalf of a panel the patient has not opened.

### Guided onboarding

- **FR-5 [must]** The chat guides the user through the required intake conversation and
  produces the structured assessment defined in the approved
  [V1 Onboarding Contract](ONBOARDING_CONTRACT.md).
- **FR-6 [must]** Local OCR extracts name, date of birth, and address from a synthetic
  identity document, and every extracted field is shown for confirmation or correction
  before it is saved.
- **FR-7 [must]** OCR failure or a partial result leaves the flow completable through
  manual entry without fabricating a value.
- **FR-8 [must]** Each defined friction trigger—long pause, upload failure, or distress
  intent—shows its mapped supportive content, while no content appears without a trigger.

### Appointment access

- **FR-9 [must]** The AI server reads the logged-in user's current, non-cancelled
  appointments through existing OpenEMR endpoints.
- **FR-10 [must]** The AI server reads provider availability, regular office hours, and
  holiday or exceptional closures through existing OpenEMR endpoints.
- **FR-11 [must]** The user can view genuinely open, future appointment slots.
- **FR-12 [must]** The user can book an open slot and receive confirmation from
  OpenEMR.
- **FR-13 [must]** The user can reschedule an existing appointment to an open slot.
- **FR-14 [must]** The user can cancel an appointment by changing its OpenEMR status;
  cancellation never deletes the record.
- **FR-15 [must]** Cancelled appointments are hidden from the patient-facing chat and
  remain available to authorized provider, office, and admin users in OpenEMR.
- **FR-16 [must]** A booking conflict or stale slot is reported clearly; the assistant
  never claims success unless OpenEMR confirms the write.
- **FR-17 [must]** Every OpenEMR read or write, including appointment, demographics, and
  assessment operations, uses an existing OpenEMR REST or FHIR endpoint. If the current
  stable release lacks a required endpoint, implementation stops at a documented
  integration gap rather than accessing the database directly.

### Chat behavior and failure

- **FR-18 [must]** The AI server streams response chunks to the iframe as they become
  available.
- **FR-19 [must]** If the AI server or external LLM is unavailable, the iframe shows an
  unavailable message and instructions for reaching OpenEMR's native scheduling UI.
- **FR-20 [must]** The model may propose an action, but appointment facts and success
  claims come only from validated OpenEMR API responses.

### Document consent and lifecycle

- **FR-21 [must]** The user grants explicit consent before any identity document is
  uploaded or processed.
- **FR-22 [must]** The AI server rejects a malformed, oversized, corrupt, or non-image
  upload before OCR and returns a clear user-facing error.
- **FR-23 [must]** Consent revocation or a deletion request stops document processing
  and purges the source image and extracted identity values in a verifiable way.
- **FR-24 [must]** The fixture generator creates a unique synthetic identity for each
  demo patient and uses the same source identity to seed OpenEMR and render that
  patient's synthetic ID image.
- **FR-25 [must]** Runtime extraction derives identity fields only from the uploaded
  image and explicit patient corrections; it does not query OpenEMR demographics, seed
  fixtures, or expected OCR labels to produce an answer.
- **FR-26 [must]** Only after the patient confirms or corrects the extracted name, date
  of birth, and address, the AI server writes those confirmed values to the logged-in
  patient's OpenEMR demographics through an existing OpenEMR endpoint. Unconfirmed
  values are never written to OpenEMR.
- **FR-27 [must]** When the onboarding assessment is completed, the AI server persists
  the structured assessment in the logged-in patient's native OpenEMR record through an
  existing OpenEMR endpoint and retains no separate durable patient record.

### Scheduling policy

- **FR-28 [must]** Appointment actions use the booking, rescheduling, cancellation,
  notice, and eligibility rules already enforced by OpenEMR; the AI server defines no
  separate scheduling policy or default.

### Runtime model

- **FR-29 [must]** For prompts accepted by the outbound privacy gate, the AI server
  obtains language-model output from Groq using the pinned model ID
  `openai/gpt-oss-120b` rather than running a language model on the Oracle AI VM.

### Session persistence

- **FR-30 [must]** During onboarding, patient answers and assessment-draft changes are
  checkpointed through an existing OpenEMR endpoint so an active flow can resume after
  an AI-server restart without persisting patient content in the AI session store.

---

## 5. Non-functional requirements

### Privacy and security

- **NFR-1 [must]** All patient, provider, appointment, and document fixtures used by the
  demo are synthetic.
- **NFR-2 [must]** Patient or provider information is never sent to an external LLM.
- **NFR-3 [must]** Before every external LLM request, the AI server checks the user
  prompt for PHI and PII.
- **NFR-4 [must]** A prompt that fails the privacy check is neither scrubbed nor sent
  externally; the server returns it locally with instructions to remove the sensitive
  content.
- **NFR-5 [must]** External LLM requests contain only the validated prompt and the
  approved, minimal scheduling fields defined in ARCHITECTURE.md.
- **NFR-6 [must]** OpenEMR access and refresh tokens remain on the AI server and are
  never exposed to iframe JavaScript, URLs, logs, traces, or analytics.
- **NFR-7 [must]** The iframe receives only a secure, HttpOnly AI-session cookie.
- **NFR-8 [must]** Application logs, traces, and analytics contain no prompt text,
  patient/provider information, delegated tokens, or raw document content.
- **NFR-9 [must]** TLS protects browser, OpenEMR, AI-server, and external-LLM traffic.

### Reliability and correctness

- **NFR-10 [must]** Replayed OAuth callbacks, stale launch state, and mismatched state
  or nonce values are rejected.
- **NFR-11 [must]** Double-submitted or concurrent booking attempts create no more than
  one confirmed appointment in OpenEMR.
- **NFR-12 [must]** Times include an explicit timezone and remain correct across DST
  boundaries.
- **NFR-13 [should]** Chat/API responses meet the original target of less than 3.0
  seconds p95 at 20 virtual users for 60 seconds.
- **NFR-14 [should]** OpenEMR scheduling reads and writes meet the original target of
  less than 1.0 second p95 at the same stated load.

### Platform and operations

- **NFR-15 [must]** Implementation pins the then-current stable OpenEMR release rather
  than tracking an unversioned image tag.
- **NFR-16 [must]** OpenEMR/MariaDB and the AI server run on separate Oracle Cloud
  Always Free VMs connected through a private OCI network.
- **NFR-17 [must]** One reserved OCI public IP and two stable sslip.io HTTPS hostnames
  expose the demo.
- **NFR-18 [must]** Core logic has at least 80% automated test coverage.
- **NFR-19 [must]** The embedded chat satisfies baseline keyboard navigation, labelled
  controls, visible focus, sufficient contrast, and non-colour-only status cues.
- **NFR-20 [must]** The default demo path requires no paid hosting, model, OCR, or
  infrastructure service.
- **NFR-21 [must]** AI_USAGE.md records runtime models, OCR engines, prompt versions,
  and material AI-assisted development.
- **NFR-22 [must]** A health endpoint reports AI-server, OpenEMR API, OCR, and external
  LLM reachability without returning sensitive configuration.
- **NFR-23 [must]** Source identity images are purged after extraction and user
  confirmation, with an independent expiry mechanism for abandoned flows.
- **NFR-24 [must]** Deployed application artifacts exclude OCR expected labels and the
  fixture generator's source identity records.
- **NFR-25 [must]** Delegated OpenEMR authorization is limited to the logged-in
  patient's required appointment access, name/date-of-birth/address demographic access,
  and structured-assessment access required by onboarding.
- **NFR-26 [must]** Groq Zero Data Retention is enabled and verified before any demo
  request is sent to `openai/gpt-oss-120b`; this setting does not weaken or bypass the
  local outbound PHI/PII gate.
- **NFR-27 [must]** The outbound gate runs a pinned Presidio Analyzer release locally
  on the Oracle AI VM with built-in PII and medical recognizers plus custom recognizers
  for OpenEMR identifiers, medical-record numbers, and project-specific healthcare
  patterns; it calls no cloud detection service.
- **NFR-28 [must]** The privacy-gate golden corpus includes every synthetic patient and
  provider fixture value plus representative PHI/PII variants, and deployment is
  blocked if any seeded sensitive value is allowed outbound.
- **NFR-29 [must]** OCR uses only a pinned local Tesseract release and pinned English
  trained data. It must reach at least 90% field-level accuracy on the synthetic-ID
  golden set before deployment; failure reopens the engine decision rather than silently
  adding PaddleOCR or a cloud OCR service.
- **NFR-30 [must]** An unexpired active chat session, including delegated OpenEMR
  authorization and the non-patient workflow cursor needed to reload its OpenEMR draft,
  survives an AI process or VM restart without requiring patient reauthorization.
- **NFR-31 [must]** The AI VM stores hashed session handles, non-patient workflow
  cursors, expiry timestamps, and AES-256-GCM-encrypted OpenEMR OAuth tokens in SQLite
  WAL mode on a persistent local volume.
- **NFR-32 [must]** The AES-256-GCM key is supplied through deployment secret storage
  outside the SQLite files and source repository, and each encrypted value uses a
  unique nonce with authenticated session metadata.
- **NFR-33 [must]** SQLite contains no prompt transcript, assessment answer, document
  content, demographic value, or other patient record, and an expiry job deletes each
  expired session and its encrypted tokens.
- **NFR-34 [must]** Caddy accepts public HTTP/HTTPS traffic for both sslip.io hostnames,
  obtains and renews their Let's Encrypt certificates, redirects HTTP to HTTPS, routes
  the `emr` hostname to OpenEMR, and routes the `chat` hostname to the private AI VM.
- **NFR-35 [must]** V1 browser acceptance covers current stable desktop and Android
  Chrome releases, with desktop Chrome prioritized; no compatibility pass is required
  for another browser family.

---

## 6. Approved external LLM payload

Each request includes only the fields needed for that turn:

- model identifier;
- system instructions;
- validated user prompt;
- current date/time and timezone;
- office-hours intervals;
- closure intervals;
- anonymous open-slot token, start, and end values;
- OpenEMR-supplied enabled-action scheduling rules when available;
- structured response schema and schema version.

Anonymous slot tokens are short-lived and are resolved to real OpenEMR resources only
inside the AI server.

---

## 7. Success metrics

| Metric | Target |
|---|---:|
| Scripted onboarding flow | Produces every required assessment field |
| OCR field accuracy | At least 90% across the synthetic golden set |
| Chat intent coverage | Every labelled intent handled without an invented commitment |
| Appointment operations | View, book, reschedule, and cancel pass end-to-end |
| Concurrent booking | Exactly one success for the final open slot |
| External privacy gate | Zero seeded PHI/PII values in captured external requests |
| Provider retention control | Groq Zero Data Retention verified before the first demo request |
| Restart recovery | An unexpired active chat resumes after an AI-server restart without reauthorization |
| Chat response | Less than 3.0 s p95 under the stated load |
| Scheduling operation | Less than 1.0 s p95 under the stated load |

The original completion-rate and 15-minute onboarding targets remain product
hypotheses until real analytics exist.

---

## 8. Open questions

- **OQ-3:** Which existing endpoints in the current stable OpenEMR release expose
  provider availability, office hours, holiday closures, rescheduling, cancellation,
  logged-in-patient demographic reads and writes, and assessment draft/completion
  persistence?
- **OQ-7:** Which native OpenEMR form, document, or other patient-record resource
  should represent the structured assessment?
- **OQ-8:** Which functional, visual, accessibility, or performance differences are
  acceptable on the lower-priority Android Chrome experience?
