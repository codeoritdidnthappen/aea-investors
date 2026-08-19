# ARCHITECTURE — Intake

**Status:** architecture updated, pre-implementation
**Last updated:** 2026-08-18

---

## 1. System overview

The demo embeds a separately deployed AI chat inside the current stable
[OpenEMR](https://github.com/openemr/openemr) frontend. OpenEMR owns authentication,
authorization, appointment state, and MariaDB. The AI server owns conversation
orchestration and ephemeral integration state. The external LLM receives only a
validated prompt and explicitly approved scheduling fields.

~~~mermaid
flowchart LR
    U["Logged-in user"]

    subgraph OCI["Oracle Cloud free tier"]
        RP["HTTPS reverse proxy<br/>reserved public IP"]

        subgraph EMR["OpenEMR VM"]
            OE["Current stable OpenEMR<br/>custom iframe module"]
            DB[("MariaDB<br/>OpenEMR-owned")]
        end

        subgraph AIS["AI VM"]
            UI["Embedded chat UI"]
            API["FastAPI"]
            LG["LangGraph orchestration"]
            PG["Outbound privacy gate"]
            TS["Encrypted token/session state"]
        end
    end

    LLM["External LLM provider"]

    U -->|"login and open chat"| OE
    RP -->|"emr hostname"| OE
    RP -->|"chat hostname"| UI
    OE --> DB
    OE -->|"OAuth/SMART code"| API
    UI -->|"AI-session cookie<br/>streamed HTTPS"| API
    API --> LG
    LG -->|"user-scoped REST/FHIR calls"| OE
    API --> TS
    LG --> PG
    PG -->|"approved fields only"| LLM
    LLM -->|"structured chunks"| LG
~~~

### Trust boundaries

1. **Browser:** holds the OpenEMR session and a separate secure AI-session cookie. It
   never holds an OpenEMR API bearer token.
2. **OCI private network:** contains OpenEMR, MariaDB, the AI server, real OpenEMR
   identifiers, user-delegated tokens, and all appointment reads and writes.
3. **External LLM:** receives only validated prompt text and approved scheduling
   context. Patient and provider information never crosses this boundary.

---

## 2. Primary request flows

### 2.1 Launch and authorization

~~~mermaid
sequenceDiagram
    participant User
    participant OpenEMR
    participant AI as AI server
    participant UI as Chat iframe

    User->>OpenEMR: Log in and open AI Chat
    OpenEMR->>OpenEMR: Verify user session and launch permission
    OpenEMR->>AI: OAuth/SMART callback with one-time code
    AI->>OpenEMR: Exchange code using registered client and PKCE
    OpenEMR-->>AI: User-scoped access and refresh tokens
    AI->>AI: Validate state/nonce and store tokens encrypted
    AI-->>UI: Establish secure HttpOnly AI session
    UI-->>User: Render chat
~~~

The authorization code is delivered to the AI server callback, not relayed through
iframe JavaScript. Replays, expired state, and mismatched state or nonce values fail
closed. The iframe receives no delegated OpenEMR credential.

This flow is based on OpenEMR's documented
[OAuth/OIDC and EHR launch support](https://github.com/openemr/openemr/blob/master/Documentation/api/AUTHENTICATION.md).

### 2.2 Scheduling turn

1. The iframe sends the user turn only to FastAPI.
2. The local privacy gate checks the prompt before any external request.
3. If the prompt contains PHI or PII, FastAPI returns it locally with instructions to
   remove the sensitive content. No external call occurs.
4. For an accepted prompt, LangGraph loads only the scheduling data required for that
   turn from existing OpenEMR endpoints.
5. The AI server converts open slots to short-lived anonymous tokens.
6. The external LLM receives the approved payload and returns structured output.
7. The AI server resolves any selected token and performs the operation through
   OpenEMR using the user's delegated token.
8. Only OpenEMR's validated response can produce a booking, rescheduling, or
   cancellation confirmation.
9. FastAPI streams response chunks to the iframe.

### 2.3 Dependency failure

If the AI server or external LLM is unavailable, the iframe displays an unavailable
message and instructions for reaching OpenEMR's native scheduling interface. There is
no parallel non-AI scheduler.

---

## 3. Components

| Component | Responsibility | Interfaces and owned data | Requirements |
|---|---|---|---|
| OpenEMR module | Add the authenticated AI Chat entry and iframe wrapper | OpenEMR module hooks; no appointment data ownership | FR-1–FR-4 |
| OpenEMR | Login, OAuth/SMART authorization, appointment system of record, role visibility | Existing REST/FHIR APIs and MariaDB | FR-3, FR-9–FR-17 |
| Chat UI | Render conversation, local error states, and streamed chunks | FastAPI only; AI-session cookie | FR-2, FR-4, FR-18–FR-19 |
| FastAPI | OAuth callback, AI-session boundary, streaming API, dependency health | Encrypted delegated tokens and ephemeral sessions | FR-3–FR-4, FR-18–FR-19; NFR-6–NFR-10 |
| LangGraph | Model the conversation and deterministic scheduling-tool transitions | Calls privacy gate, OpenEMR adapter, and LLM adapter | FR-5, FR-8–FR-20 |
| PrivacyGate | Block unsafe prompts and enforce outbound schema | No retained prompt data | NFR-2–NFR-5, NFR-8 |
| OpenEMR adapter | Translate scheduling tools into existing API calls | No database access and no appointment persistence | FR-9–FR-17 |
| LLM adapter | Send approved payloads and validate structured streamed output | External provider connection only | FR-18, FR-20; NFR-2–NFR-5 |
| Local OCR adapter | Enforce consent, validate uploads, extract synthetic identity fields, and purge source images | Request-duration image bytes and confirmed output | FR-6–FR-7, FR-21–FR-23 |
| OCI reverse proxy | Terminate TLS and route two sslip.io hostnames | TLS certificates and routing configuration | NFR-9, NFR-16–NFR-17 |

---

## 4. OpenEMR data access

OpenEMR is the only owner of appointment state. The AI server must not connect to
MariaDB, import OpenEMR tables, or maintain its own schedule.

| User behavior | OpenEMR operation | Data returned to chat |
|---|---|---|
| Check appointments | Read user's active appointments | Non-cancelled appointments only |
| Find a time | Read availability, office hours, and closures | Genuine future open slots |
| Book | Create appointment | OpenEMR confirmation or conflict |
| Reschedule | Update existing appointment to an open slot | OpenEMR confirmation or conflict |
| Cancel | Update appointment status to cancelled | Cancellation confirmation |

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

LangGraph separates model reasoning from authoritative actions:

~~~mermaid
flowchart LR
    T["User turn"] --> P{"Privacy gate"}
    P -->|"rejected"| R["Local correction response"]
    P -->|"accepted"| I["Intent and preference extraction"]
    I --> C["Load minimal OpenEMR context"]
    C --> A["Anonymous scheduling payload"]
    A --> M["External LLM"]
    M --> V{"Schema validation"}
    V -->|"invalid"| F["Safe failure"]
    V -->|"valid"| X["Deterministic OpenEMR tool"]
    X --> O["Stream confirmed response"]
~~~

The model can interpret preferences and select anonymous candidates. It cannot invent
an appointment fact, select an OpenEMR identifier directly, or report a successful
write before OpenEMR confirms it.

The structured assessment is accumulated inside the AI server. Sensitive field values
are inserted by deterministic local graph nodes after model processing, so the
external model does not need patient information to produce the final record shape.

### Approved external request shape

~~~json
{
  "model": "configured-model-id",
  "messages": [
    {
      "role": "system",
      "content": "Scheduling assistant instructions"
    },
    {
      "role": "user",
      "content": "Validated user prompt"
    }
  ],
  "scheduling_context": {
    "current_datetime": "2026-08-18T14:30:00-05:00",
    "timezone": "America/Chicago",
    "office_hours": [
      {
        "day_of_week": "monday",
        "opens_at": "09:00",
        "closes_at": "17:00"
      }
    ],
    "closures": [
      {
        "starts_at": "2026-09-07T00:00:00-05:00",
        "ends_at": "2026-09-08T00:00:00-05:00"
      }
    ],
    "open_slots": [
      {
        "slot_token": "slot_A",
        "starts_at": "2026-08-25T13:00:00-05:00",
        "ends_at": "2026-08-25T13:30:00-05:00"
      }
    ]
  },
  "scheduling_rules": {
    "minimum_booking_notice_minutes": 1440,
    "booking_enabled": true,
    "rescheduling_enabled": true,
    "cancellation_enabled": true
  },
  "response_format": {
    "type": "json_schema",
    "schema_version": "1"
  }
}
~~~

Each request omits unused sections. Slot tokens are random, single-purpose,
short-lived, and resolvable only inside the AI server.

---

## 6. Data ownership and lifecycle

| Data | Owner | Storage and lifecycle |
|---|---|---|
| Users, roles, providers | OpenEMR | OpenEMR/MariaDB |
| Appointments, availability, office hours, closures | OpenEMR | OpenEMR/MariaDB |
| Cancelled appointment history | OpenEMR | Retained under OpenEMR policy |
| OAuth access and refresh tokens | AI server | Encrypted server-side; lifetime follows token/session policy |
| AI session | AI server | Opaque and short-lived; storage implementation open |
| User prompt | AI server | Request/conversation duration only; never logged |
| Anonymous slot mapping | AI server | Short-lived and deleted after use or expiry |
| Synthetic ID image | AI server/local OCR | Until extraction and confirmation, then purged |
| External LLM request | External provider | Approved payload only; provider retention still to be selected |

No separate appointment entity or scheduling database exists on the AI server.

---

## 7. Technology choices

| Choice | Rationale |
|---|---|
| Current stable OpenEMR | Deliberately demonstrates modern AI inside an older EHR UI and supplies authentication and scheduling |
| Custom iframe module | Keeps the user inside OpenEMR while allowing a separately deployed chat application |
| Python + FastAPI | Provides OAuth callbacks, typed APIs, async external calls, and streamed HTTP responses |
| LangGraph | Makes privacy, model, tool, validation, and fallback transitions explicit |
| Existing OpenEMR REST/FHIR APIs | Preserves OpenEMR authorization and data ownership without database coupling |
| OAuth/SMART authorization code | Delegates only the logged-in user's allowed scope |
| Two OCI free VMs | Separates the EHR and AI server while meeting the zero-hosting-cost constraint |
| sslip.io + reserved public IP | Supplies stable demo hostnames without purchasing a domain |
| Let's Encrypt | Supplies browser-trusted TLS without certificate cost |
| External LLM adapter | Allows a provider to be selected later without changing orchestration |

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

---

## 9. OCI deployment

One reserved OCI public IP serves both HTTPS hostnames:

- emr.<dashed-public-ip>.sslip.io routes to OpenEMR on VM 1;
- chat.<dashed-public-ip>.sslip.io routes through the reverse proxy to FastAPI on VM 2
  over the OCI private network.

VM 1 runs the reverse proxy, the pinned OpenEMR container, and MariaDB with persistent
volumes. VM 2 runs the chat UI and Python/FastAPI/LangGraph service. Only the reverse
proxy accepts public application traffic. OCI network rules limit MariaDB and FastAPI
origin ports to private-network callers. Let's Encrypt supplies individual certificates
for both sslip.io hostnames.

---

## 10. Open architecture questions

- Which existing endpoints on the pinned OpenEMR release satisfy every required
  availability, office-hours, closure, rescheduling, and cancellation operation?
- Which external LLM provider, model, and retention policy satisfy the privacy gate?
- Which PHI/PII detector and golden test set make the outbound guarantee measurable?
- Which encrypted server-side store will hold OAuth tokens and AI sessions?
- Which reverse proxy will manage hostname routing and Let's Encrypt renewal?
- Which browser/iframe cookie matrix must the sslip.io deployment support?
- Which OCR engine and confidence threshold meet the original extraction target?
