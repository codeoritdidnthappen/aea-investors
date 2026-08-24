# ARCHITECTURE — Intake

**Status:** architecture updated, pre-implementation
**Last updated:** 2026-08-18

---

## 1. System overview

The demo embeds a separately deployed AI chat inside the current stable
[OpenEMR](https://github.com/openemr/openemr) frontend. OpenEMR owns authentication,
authorization, appointment state, demographics, structured assessments, and MariaDB.
The AI server owns conversation orchestration and ephemeral integration state. Groq's
`openai/gpt-oss-120b` receives only a validated prompt and explicitly approved scheduling
fields, with Zero Data Retention enabled.

~~~mermaid
flowchart LR
    U["Logged-in user"]

    subgraph OCI["Oracle Cloud free tier"]
        RP["Caddy HTTPS reverse proxy<br/>reserved public IP"]

        subgraph EMR["OpenEMR VM"]
            OE["Current stable OpenEMR<br/>custom iframe module"]
            DB[("MariaDB<br/>OpenEMR-owned")]
        end

        subgraph AIS["AI VM"]
            UI["Embedded chat UI"]
            API["FastAPI"]
            MT["ModelTurnService<br/>one schema'd tool call per turn"]
            LM["Local model server<br/>pinned instruct model"]
            PG["Local Presidio<br/>privacy gate"]
            TS["SQLite WAL<br/>encrypted token/session state"]
        end
    end

    LLM["Groq<br/>openai/gpt-oss-120b<br/>Zero Data Retention"]

    U -->|"login and open chat"| OE
    RP -->|"emr hostname"| OE
    RP -->|"chat hostname"| UI
    OE --> DB
    OE -->|"OAuth/SMART code"| API
    UI -->|"AI-session cookie<br/>streamed HTTPS"| API
    API -->|"every turn"| MT
    MT -->|"patient text; PHI permitted"| LM
    LM -->|"one tool call"| MT
    MT -->|"validated values,<br/>user-scoped REST/FHIR calls"| OE
    API --> TS
    MT -->|"model's restatement only"| PG
    PG -->|"approved fields only"| LLM
    LLM -->|"answer"| MT
~~~

### Trust boundaries

1. **Browser:** holds the OpenEMR session and a separate secure AI-session cookie. It
   never holds an OpenEMR API bearer token.
2. **OCI private network:** contains OpenEMR, MariaDB, the AI server, the local model
   server, real OpenEMR identifiers, user-delegated tokens, and all appointment reads
   and writes. The patient's own words stay inside this boundary; the local model is
   here precisely so that they can.
3. **Groq:** receives only a canonical restatement of a general-knowledge question,
   composed by this codebase from a schema'd field and scanned by the privacy gate, with
   Zero Data Retention enabled. Patient and provider information never crosses this
   boundary, and neither does anything the patient typed.

---

## 2. Primary request flows

### 2.1 Launch and authorization

~~~mermaid
sequenceDiagram
    participant P as Patient
    participant OpenEMR
    participant UI as Chat panel (iframe)
    participant AI as AI server

    P->>OpenEMR: Sign in to the patient portal
    OpenEMR-->>P: Portal dashboard (panel closed, no src yet)
    Note over UI: data-src holds the launch URL.<br/>Nothing loads until the tile is opened (FR-32).
    P->>UI: Open the AI Chat tile
    UI->>AI: GET /oauth/launch
    alt Live ai_session
        AI-->>UI: Chat page, no authorization round trip
    else No session
        AI->>OpenEMR: 302 to authorize (PKCE)
        OpenEMR-->>P: Sign-in / consent, broken out to top level
        P->>OpenEMR: Credentials and consent
        OpenEMR->>AI: GET /oauth/callback with one-time code
        AI->>OpenEMR: Exchange code with PKCE
        OpenEMR-->>AI: Patient-scoped access and refresh tokens
        AI->>AI: Validate state/nonce, encrypt tokens, issue AI session
        AI-->>P: Sec-Fetch-Dest document -> portal dashboard (FR-31, ADR-8)
    end
    P->>UI: Chat turn
    UI->>AI: POST /api/chat (Origin-checked)
    P->>OpenEMR: Sign out
    OpenEMR->>AI: POST /api/logout, session deleted
~~~

The authorization code is delivered to the AI server callback, not relayed through
iframe JavaScript. Replays, expired state, and mismatched state or nonce values fail
closed. The iframe receives no delegated OpenEMR credential.

**Constraint — the portal session cannot authorize the chat; the second sign-in stays.**
OpenEMR 8.3.0 will not accept an authenticated patient-portal session at
`/oauth2/default/authorize`. A patient who has just signed in to the portal signs in a
second time when they first open the chat, and consents again on every authorization.
This is a property of the release, not of this deployment, and no configuration removes
it. Established by [TICK-056](evidence/TICK-056/FINDING.md), which exercised it live.

The cause is not cookie scoping — the `PortalOpenEMR` cookie is issued with `path=/` and
*is* delivered to the authorization endpoint; nothing there reads it. The only path in
the release that skips the login is the SMART EHR launch, and it resolves its user from
the *core* session's `authUserID` against the `users` table. A portal session has no
`authUserID`, and a patient has no `users` row. The release states the limitation itself:
`// for now we only handle in-ehr launch for providers not patients`
(`src/RestControllers/AuthorizationController.php:1921`). Consent is separately
unconditional: `oauth_trusted_user` is never consulted before the consent screen, so
prior approval does not suppress it.

Do not reach for the EHR-launch skip path to close this. TICK-056 exercised it: it mints
a token whose `sub` is a `users` row while the patient context comes only from the launch
token, so the identity that authenticates is not the identity the token grants access to.
That would dissolve the boundary TICK-028 established between the patient's portal
session and delegated API authorization. Re-check this constraint on any OpenEMR upgrade;
the upstream comment reads like an acknowledged gap.

**Invariant — the chat is a panel, never a landing page.** An authorization that
completes at top level lands the patient on the portal dashboard. One that completes
inside the chat panel loads the chat in that panel. The patient is never left on the
standalone chat page and is never returned to whatever page they were on when the session
ended. Fixed by [ADR-8](#adr-8--the-chat-is-a-panel-never-a-landing-page).

The rule is stated over the flow's *position*, not the patient's intent. "Did they type a
password?" is not something the callback can know: the SMART EHR-launch skip path
completes the whole exchange with no prompt at all, whether the flow is running at top
level or in the panel, and a future release that reuses a session would do the same.
Position is knowable, and it is what actually determines whether landing on a full-page
chat would strand the patient. (Note that a live OAuth2 *provider* session does not skip
the prompt in 8.3.0 — `oauthAuthorizationFlow()` redirects to `/provider/login`
unconditionally, verified in [TICK-056](evidence/TICK-056/FINDING.md). The rule does not
depend on that either way.)

Three mechanics make it work, and each is load-bearing:

1. **The chat authorizes only when opened.** The dashboard's AI Chat panel must not carry
   a live `src` at render. A hidden iframe still loads, so an `/oauth/launch` `src` present
   at render starts an OAuth flow for a panel the patient never opened — and when that flow
   needs a login, the breakout script navigates the *top-level* window off the dashboard.
   The patient is thrown off the page they just signed in to, having clicked nothing
   (FR-32). This is also what makes the rule safe to state absolutely: a dashboard that
   spawns no chat iframe at render cannot be redirected into itself.
2. **`/oauth/launch` short-circuits on a live session, subject to the same rule.** With a
   valid `ai_session` it skips the authorization round trip — serving the chat when it is
   running in the panel, and redirecting to the dashboard when it is running at top level.
   The short-circuit is not an exception to the invariant: a live session reached at top
   level must not be answered with the full-page chat.
3. **The callback reads its own position from `Sec-Fetch-Dest`, never from a parameter.**
   Browsers send `document` for a top-level navigation and `iframe` for one into a frame,
   so the server resolves the destination itself with no client-side interstitial at all.
   An absent or unrecognised value is treated as top level, matching ADR-8: the dashboard
   strands nobody, since a patient sent there is one click from the chat, whereas the
   full-page chat leaves them with the portal gone. Chrome, the only supported target
   (NFR-19, NFR-35), always sends the header, so that default should not run in practice.
   No `next=` or return URL is involved, and none may be added.

Note that `/oauth/launch` is the patient-facing entry point, not a development affordance:
it is the panel's `src`, taken from `AEAI_PORTAL_CHAT_URL` when set and otherwise from
`PortalChatController::DEFAULT_CHAT_LAUNCH_URL`. Nothing else depends on its top-level
behaviour — the probe scripts under `scripts/` do not use it, driving OpenEMR's
`/oauth2/default/authorize` with their own `http://localhost:8910/callback` and exchanging
tokens themselves.

Two settings are involved in the destination and they are **not** interchangeable, however
similar they look. The post-login redirect target decides where the patient lands. The chat
origin allowlist decides which `Origin` may call `POST /api/chat`, and is the only CSRF
defense on that route, because the AI session cookie is `SameSite=None` for the cross-site
iframe. Collapsing them into one value — as `AI_SESSION_SUCCESS_REDIRECT_URI` originally did
— means any change to where a patient lands silently rewrites who may call the chat API.

This flow is based on OpenEMR's documented
[OAuth/OIDC and EHR launch support](https://github.com/openemr/openemr/blob/master/Documentation/api/AUTHENTICATION.md).

### 2.2 Chat turn

Every turn takes this path. There is no second one: no mode detection, no phrase
matching, and no deterministic handler that a turn can be routed to instead
(LOCAL_LLM_SPEC D9, D12).

1. The iframe sends the user turn only to FastAPI.
2. FastAPI checks the session and the request `Origin`, and inspects nothing about what
   the patient wrote.
3. `ModelTurnService` sends the turn to the **local** model, which may see PHI, together
   with the conversation state this session already recorded — the pending confirmation,
   the anonymous tokens it was offered, and a bounded transcript.
4. The model returns exactly one tool call under a strict schema.
5. The AI server validates every field independently of the model, and reads validated
   values back to the patient for confirmation before any write (D6).
6. A confirmed write executes through the OpenEMR adapters using the user's delegated
   token; open slots and existing appointments are exchanged for short-lived anonymous
   tokens, and only a token the AI server itself issued resolves.
7. Only OpenEMR's validated response can produce a booking or cancellation confirmation.
8. A general-knowledge question is the one branch that leaves the deployment. The
   outbound payload is composed by this codebase from the model's own canonical
   restatement, never from the patient's words, and the privacy gate scans it before it
   goes (D3, D4, D14).
9. FastAPI streams response chunks to the iframe.

### 2.3 Dependency failure

**Model-server availability is chat availability.** D12 deleted the deterministic
handlers rather than keeping them as an outage fallback, because a path nobody exercises
rots and then produces a bad parse in a medical record at the worst possible moment. The
consequence is accepted and handled explicitly:

- When the model server is unreachable or unconfigured, the chat replies that the
  assistant is temporarily unavailable and that the patient's portal still works. It
  names no internal component.
- No degraded path runs. Because a turn cannot proceed past the routing inference, no
  tool executes and **no write is attempted** — including a change that was already
  validated and read back and is only awaiting a confirmation.
- `/health` reports `model_server` alongside `openemr_api`, `ocr` and `external_llm`, so
  the outage is visible to monitoring before a patient finds it. An unconfigured model
  server reports `unavailable` too, since from the patient's side it is the same outage.
- An unreachable *external* LLM is a different, much smaller failure: it costs
  general-knowledge answers only, and has its own message.

There is no parallel non-AI scheduler. The next step offered to the patient is OpenEMR's
own patient-portal scheduling screen, which is unaffected by an AI-server outage.

---

## 3. Components

| Component | Responsibility | Interfaces and owned data | Requirements |
|---|---|---|---|
| OpenEMR module | Add the authenticated AI Chat dashboard tile and panel, hold the launch URL in `data-src` until the patient opens it, and end the AI session on portal sign-out | OpenEMR module hooks; no appointment data ownership | FR-1–FR-4, FR-32 |
| OpenEMR | Login, OAuth/SMART authorization, appointment system of record, role visibility | Existing REST/FHIR APIs and MariaDB | FR-3, FR-9–FR-17 |
| Chat UI | Render conversation, local error states, and streamed chunks | FastAPI only; AI-session cookie | FR-2, FR-4, FR-18–FR-19 |
| FastAPI | OAuth launch and callback, position-resolved destination, AI-session boundary and logout, streaming API, dependency health | SQLite WAL session plumbing and encrypted delegated tokens | FR-1, FR-3–FR-4, FR-18–FR-19, FR-31; NFR-6–NFR-10, NFR-30–NFR-33 |
| Local model server | Serve the pinned instruct model over an OpenAI-compatible API. Ollama in development, vLLM when deployed | Model weights pinned by name and digest; reachable only on the internal network | FR-5, FR-33–FR-34 |
| `ModelTurnService` | Own every turn: build the model's context, run one routing inference, validate and execute the single tool call it returns, and stream the reply | In-process, request-scoped conversation state keyed by AI-session handle; discarded on logout | FR-5, FR-8–FR-20, FR-33 |
| Tool surface and write validation | Publish the schema-constrained tools the model may call, validate every proposed field independently of the model, and render the confirmation read-back | Validated values only; a tool executes with the validator's values, never the model's | FR-9–FR-17, FR-21–FR-23; NFR-36 |
| PrivacyGate | Run local Presidio and enforce the outbound schema on the one payload that leaves | Pinned local models and custom recognizers; no retained prompt data | NFR-2–NFR-5, NFR-8, NFR-27–NFR-28 |
| OpenEMR adapter | Translate scheduling tools into existing API calls | No database access and no appointment persistence | FR-9–FR-17 |
| Groq adapter | Send the model's canonical restatement of a general-knowledge question to `openai/gpt-oss-120b` and return the answer | Groq connection only; API key remains server-side; never receives patient text | FR-18, FR-20, FR-29; NFR-2–NFR-5, NFR-26 |
| Local OCR adapter | Enforce consent, validate uploads, extract synthetic identity fields with pinned local Tesseract, and purge source images | Request-duration image bytes and confirmed output | FR-6–FR-7, FR-21–FR-23, NFR-29 |
| Caddy | Terminate TLS, automate Let's Encrypt, and route two sslip.io hostnames | Caddyfile, certificates, and routing configuration | NFR-9, NFR-16–NFR-17, NFR-34 |

---

## 4. OpenEMR data access

OpenEMR is the only owner of persisted patient data, including appointment state,
demographics, and structured assessments. The AI server must not connect to MariaDB,
import OpenEMR tables, maintain its own schedule, or create a parallel patient record.

| User behavior | OpenEMR operation | Data returned to chat |
|---|---|---|
| Check appointments | Read user's active appointments | Non-cancelled appointments only |
| Find a time | Read availability, office hours, and closures | Genuine future open slots |
| Book | Create appointment | OpenEMR confirmation or conflict |
| Reschedule | Update existing appointment to an open slot | OpenEMR confirmation or conflict |
| Cancel | Update appointment status to cancelled | Cancellation confirmation |
| Confirm identity | Update the logged-in patient's demographics | Confirmed name, date of birth, and address |
| Advance assessment | Create or update the native draft assessment | OpenEMR checkpoint confirmation |
| Complete assessment | Finalize the native assessment record | OpenEMR persistence confirmation |

Cancellation never calls a delete operation. OpenEMR retains the record for authorized
provider, office, and admin users. The patient-facing chat filters it from ordinary
appointment results.

The current Standard REST API documents appointment create, read, update, and search
capabilities. Exact existing endpoint coverage for provider availability, office
hours, holiday closures, rescheduling, and cancellation must be verified against the
pinned stable release before implementation. A missing endpoint is an integration
blocker; direct database access is not an allowed workaround.

Reference:
[OpenEMR Standard API](https://github.com/openemr/openemr/blob/master/Documentation/api/STANDARD_API.md).

---

## 5. AI orchestration

`ModelTurnService` separates model reasoning from authoritative actions:

~~~mermaid
flowchart LR
    T["User turn"] --> L["Local LLM: one tool call"]
    L --> V{"Schema and field validation"}
    V -->|"invalid"| F["Safe failure"]
    V -->|"valid"| X["Deterministic OpenEMR tool"]
    X --> O["Stream confirmed response"]
    L -->|"ask_general_knowledge"| A["Payload composed from the model's restatement"]
    A --> P{"Privacy gate"}
    P -->|"rejected"| R["Withheld; reported to the patient"]
    P -->|"accepted"| M["External LLM"]
    M --> O
~~~

Every turn goes to the *local* model first, which may see PHI, and its one tool call
selects what happens (LOCAL_LLM_SPEC D9). The model can interpret preferences and
select anonymous candidates. It cannot invent an appointment fact, select an OpenEMR
identifier directly, or report a successful write before OpenEMR confirms it.

The external model is reached from exactly one branch and receives only a canonical
restatement the local model composed (D13, D14). The privacy gate is no longer the sole
control on what leaves: the outbound payload cannot be constructed from a patient turn
at all, and the gate screens the constructed payload as a second, independent check
(D3, D4).

Patient answers and assessment-draft changes are checkpointed to the logged-in
patient's native OpenEMR record during the conversation. The AI server keeps only
request-duration patient values in memory and stores a non-patient workflow cursor in
SQLite. After restart it reloads the draft from OpenEMR. Sensitive field values are
written by local code from validated values after model processing, so the external
model does not need patient information to produce the final record shape.

### Approved external request shape

Both strings below are composed by this codebase: the system message is a fixed
constant in `ai_server/privacy/gate.py`, and the user message is the local model's
schema'd restatement. Neither is anything the patient typed.

~~~json
{
  "model": "openai/gpt-oss-120b",
  "messages": [
    {
      "role": "system",
      "content": "General-knowledge instructions (a fixed local constant)"
    },
    {
      "role": "user",
      "content": "The local model's canonical restatement of the question"
    }
  ]
}
~~~

`scheduling_context`, `scheduling_rules` and `response_format` were removed by TICK-064.
Scheduling planning is local now (D13), so no outbound payload describes scheduling, and
a general-knowledge answer is prose rather than a schema.

Each request omits unused sections. Slot tokens are random, single-purpose,
short-lived, and resolvable only inside the AI server.

---

## 6. Data ownership and lifecycle

| Data | Owner | Storage and lifecycle |
|---|---|---|
| Users, roles, providers | OpenEMR | OpenEMR/MariaDB |
| Patient demographics | OpenEMR | Confirmed name, date of birth, and address persist in OpenEMR/MariaDB |
| Structured assessment | OpenEMR | Draft changes and completion persist in the native patient record through an existing API |
| Appointments, availability, office hours, closures | OpenEMR | OpenEMR/MariaDB |
| Cancelled appointment history | OpenEMR | Retained under OpenEMR policy |
| OAuth access and refresh tokens | AI server | AES-256-GCM-encrypted SQLite columns; deleted with session expiry |
| AI session | AI server | Hashed handle, non-patient cursor, and expiry in local SQLite WAL; durable across restart |
| User prompt | AI server | Request/conversation duration only; never logged |
| Anonymous slot mapping | AI server | Short-lived and deleted after use or expiry |
| Synthetic ID image | AI server/local OCR | Until extraction and confirmation, then purged |
| External LLM request | Groq | Approved payload only; Zero Data Retention enabled |

No separate appointment entity or scheduling database exists on the AI server.

---

## 7. Technology choices

| Choice | Rationale |
|---|---|
| Current stable OpenEMR | Deliberately demonstrates modern AI inside an older EHR UI and supplies authentication and scheduling |
| Custom iframe module | Keeps the user inside OpenEMR while allowing a separately deployed chat application |
| Python + FastAPI | Provides OAuth callbacks, typed APIs, async external calls, and streamed HTTP responses |
| Local model server + one schema'd tool call | Handles every phrasing without hand-coding it, while keeping PHI inside the deployment; the tool schema and field validators keep the model's output from reaching a record unchecked |
| Existing OpenEMR REST/FHIR APIs | Preserves OpenEMR authorization and data ownership without database coupling |
| OAuth/SMART authorization code | Delegates only the logged-in user's allowed scope |
| Two OCI free VMs | Separates the EHR and AI server while meeting the zero-hosting-cost constraint |
| sslip.io + reserved public IP | Supplies stable demo hostnames without purchasing a domain |
| Caddy + Let's Encrypt | Combines hostname routing, browser-trusted TLS, and automatic certificate renewal without certificate cost |
| Groq + `openai/gpt-oss-120b` | Answers general-knowledge questions, which carry no patient context, without operating a second inference workload |
| Local Presidio Analyzer | Supplies free, extensible PII and medical detection within the Oracle AI VM |
| Local Tesseract OCR | Supplies controlled synthetic-ID extraction without a cloud dependency |
| SQLite WAL + AES-256-GCM | Persists session plumbing across restart without a separate database service |

---

## 8. Key decisions and trade-offs

### ADR-1 — OpenEMR replaces the standalone frontend

**Decision:** OpenEMR hosts login and the iframe entry point.
**Consequence:** Less greenfield UI work, but module and version compatibility become
part of the test surface.

### ADR-2 — Browser isolation

**Decision:** The iframe calls only FastAPI.
**Consequence:** OpenEMR bearer tokens stay outside browser JavaScript; the AI server
becomes the integration and authorization enforcement point.

### ADR-3 — API-only EHR access

**Decision:** All scheduling reads and writes use existing OpenEMR endpoints.
**Consequence:** Missing API coverage blocks a feature instead of being bypassed with
database access.

### ADR-4 — Cancellation preserves history

**Decision:** Cancellation is a status update and never a delete.
**Consequence:** The patient chat filters cancelled records while staff roles retain
history in OpenEMR.

### ADR-5 — Hard outbound privacy gate

**Decision:** Reject unsafe prompts rather than scrub them.
**Consequence:** False positives may interrupt conversation, but no altered sensitive
prompt is silently forwarded.

### ADR-6 — Synthetic-only deployment

**Decision:** The demo uses no real PHI.
**Consequence:** The architecture demonstrates privacy controls but does not claim
production healthcare compliance.

### ADR-7 — Native scheduling fallback

**Decision:** Dependency failure directs the user to OpenEMR's existing scheduler.
**Consequence:** There is one scheduling source of truth and no duplicate fallback UI.

### ADR-8 — The chat is a panel, never a landing page

**Decision:** An authorization completing at top level lands on the portal dashboard; one
completing inside the panel loads the chat there. The destination takes no input: no
`next=`, `redirect=`, or equivalent return-URL parameter may be added to reinstate "send
them back where they were", however reasonable that looks as a usability improvement.
Position is read from `Sec-Fetch-Dest`, defaulting to top level when absent. The redirect
target and the `POST /api/chat` origin allowlist are configured separately, so the
destination can never be changed by editing a CSRF setting, or vice versa.
**Consequence:** The patient can never be stranded on a full-page chat, which is what FR-2
and FR-31 ask for. A top-level visit to the chat's own URL lands on the dashboard rather
than the chat — accepted deliberately, since the chat is not a standalone application.
OpenEMR's native portal login already lands on `portal/home.php` and is not modified by
this decision. **This flow is settled. Do not change it.** A future ticket proposing to
land a top-level authorization anywhere other than the dashboard is rejected on this ADR
alone, regardless of the reason given.

---

## 9. OCI deployment

One reserved OCI public IP serves both HTTPS hostnames:

- emr.<dashed-public-ip>.sslip.io routes to OpenEMR on VM 1;
- chat.<dashed-public-ip>.sslip.io routes through the reverse proxy to FastAPI on VM 2
  over the OCI private network.

VM 1 runs Caddy, the pinned OpenEMR container, and MariaDB with persistent volumes.
VM 2 runs the chat UI, the Python/FastAPI service, and the local model server, plus a
persistent local volume for SQLite session state. Only the reverse
proxy accepts public application traffic. OCI network rules limit MariaDB and FastAPI
origin ports to private-network callers. Let's Encrypt supplies individual certificates
for both sslip.io hostnames.

---

## 10. Open architecture questions

- Which existing endpoints on the pinned OpenEMR release satisfy every required
  availability, office-hours, closure, rescheduling, and cancellation operation?
- Which supported extension hook adds the iframe to the patient portal on the pinned
  OpenEMR release?
- Which native OpenEMR form, document, or other patient-record resource represents the
  structured assessment?
- Which Android Chrome platforms and acceptable degradation belong in the acceptance matrix?
