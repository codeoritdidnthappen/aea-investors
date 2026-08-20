# TICK-001 — OpenEMR v8.3.0 endpoint matrix

**Recorded:** 2026-08-18 (runtime probe completed 2026-08-19)
**Decision:** local foundation and documented Standard-API integration may proceed with
synthetic data. Do not use a database workaround or implement an operation until its
route and behavior are verified.

## Local-development scope correction

This matrix originally treated production patient-scoped authorization as a block on
all implementation. That was too broad. Local development uses OpenEMR's documented
Standard REST API, a locally registered OAuth client, and synthetic test users. The
`user/*` Standard-API scopes are acceptable only in that isolated local environment;
they do not establish the eventual patient-scoped production authorization boundary.

TICK-005 may therefore build the local FastAPI/LangGraph foundation and test harness.
The rows below remain gates only for the scheduling or assessment operation they name.

## Pinned upstream inputs

| Input | Immutable reference | Verification |
|---|---|---|
| Release | [OpenEMR v8.3.0](https://github.com/openemr/openemr/releases/tag/v8_3_0), published 2026-08-18 | Annotated tag object `71da6605dc3bcc1405ee9a65cb9f579ecbc8a1c6`; downloaded source directory `openemr-openemr-71da660` |
| Source used for this review | [tagged source](https://github.com/openemr/openemr/tree/v8_3_0) | `swagger/openemr-api.yaml` SHA-256 `773ed01a53b2a0f8b0ab6afb157896167e100bad73124c3ab0354e0010d89b16` |
| amd64 image | `openemr/openemr:8.3.0@sha256:4758f222801a0e3df5510da1b0920fb057d07512d8aedcfa2c44e39b04cd8c35` | Docker Hub tag metadata retrieved 2026-08-18 |
| arm64 image | `openemr/openemr:8.3.0@sha256:0dcde173381adadcc39884e7c7557e1f674eb241722d65400e4da1b6f52f2a00` | Docker Hub tag metadata retrieved 2026-08-18 |

All paths below are relative to `https://{host}/apis/default`. `patient/*` means a
SMART patient-context token. `user/*` means the access available to an OpenEMR user;
it is not an acceptable substitute for the logged-in-patient constraint in NFR-25.

## Matrix

| Required operation | Endpoint / method on v8.3.0 | Least stated scope / authorization | Redacted evidence | Result and gap |
|---|---|---|---|---|
| OAuth/SMART EHR launch (FR-3) | `GET /oauth2/default/authorize` with `launch`; `POST /oauth2/default/token` | `openid launch launch/patient` plus API resource scopes; `offline_access` for refresh | Tagged [authentication guide](https://github.com/openemr/openemr/blob/v8_3_0/Documentation/api/AUTHENTICATION.md#ehr-launch-flow) shows `iss`, `[REDACTED_LAUNCH]`, authorization-code exchange, and token response with `patient: "[REDACTED_UUID]"`. | **Supported.** Register a confidential client and prove PKCE/state/nonce behavior in the deployed probe. |
| Read current appointments (FR-9) | `GET /fhir/Appointment` and `GET /fhir/Appointment/{uuid}` | `api:fhir patient/Appointment.rs` | [Route source](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php#L96-L117) passes the bound patient UUID for patient requests. Redacted response surface: `{ "resourceType":"Bundle", "entry":[{"resource":{"resourceType":"Appointment","id":"[REDACTED_UUID]","status":"booked"}}] }`. | **Supported for reads.** Filter cancelled statuses in the AI server before presenting results. |
| Create appointment / book (FR-12) | `POST /api/patient/{pid}/appointment` | `user/appointment.cruds`; OpenEMR ACL `patients/appt` | Local v8.3.0 synthetic probe returned `200` and `{ "id": "[REDACTED_EVENT_ID]" }` for the required body fields `pc_catid`, `pc_title`, `pc_duration`, `pc_hometext`, `pc_apptstatus`, `pc_eventDate`, `pc_startTime`, `pc_facility`, `pc_billing_location`, and `pc_aid`; scoped list and single-record reads also returned `200`. | **Supported locally.** Production patient-scoped authorization remains unproven. |
| Provider availability (FR-10) | None found | — | Tagged route inventory has no `Schedule` or `Slot` resource/route. | **Implementation-blocking API gap.** Appointment reads cannot establish genuinely open slots. No database query/workaround is permitted. |
| Regular office hours (FR-10) | None found | — | Tagged Standard/FHIR route inventory has no office-hours/calendar-hours endpoint. | **Implementation-blocking API gap.** No database query/workaround is permitted. |
| Holiday or exceptional closures (FR-10) | None found | — | Tagged Standard/FHIR route inventory has no closure/holiday endpoint. | **Implementation-blocking API gap.** No database query/workaround is permitted. |
| Reschedule appointment (FR-13) | None found | — | Exact route checks found no `PUT` or `PATCH /api/patient/:pid/appointment`, nor any mutating FHIR `Appointment` route. The Standard API exposes only appointment `GET`, `POST`, and `DELETE` routes ([source](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_standard.inc.php#L397-L432)). | **Implementation-blocking API gap.** The advertised OAuth scope cannot create a missing route. Do not emulate reschedule with delete/create. |
| Cancel by status, retain history (FR-14) | None found | — | The only appointment mutation for an existing record is `DELETE /api/patient/{pid}/appointment/{eid}` ([source](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_standard.inc.php#L422-L426)); its controller calls `deleteAppointmentRecord`. | **Implementation-blocking semantic gap.** `DELETE` conflicts with FR-14; do not call it and do not update `pc_event` through MariaDB. |
| Read logged-in patient demographics | `GET /fhir/Patient/{uuid}` | `api:fhir patient/Patient.rs` | [Route source](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php#L610-L626) rejects a UUID other than the bound patient. Redacted response surface: `{ "resourceType":"Patient", "id":"[REDACTED_UUID]", "name":[{"family":"[REDACTED]"}], "birthDate":"[REDACTED_DATE]", "address":[{"line":["[REDACTED]" ]}] }`. | **Supported for reads.** |
| Write confirmed demographics (FR-26) | `PUT /api/patient/{uuid}` or `PUT /fhir/Patient/{uuid}` | Standard: `api:oemr user/patient.crus`; FHIR route additionally requires ACL `patients/demo` | [Standard route](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_standard.inc.php#L92-L97); [FHIR route](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php#L569-L577). Unlike the FHIR `GET`, the FHIR `PUT` has no patient-binding branch. | **Resolved — READ-ONLY (TICK-028, `evidence/TICK-028/BINDING_MATRIX.md`).** A live authorization_code+PKCE patient token was probed against the Standard `PUT /api/patient/{uuid}` route on both its own chart and a second patient's chart; both returned `403 user role does not have access to the resource`. The patient role cannot write demographics via this route at all, own chart included. The product performs no demographic write as the patient on v8.3.0; TICK-016's staff-credential write path must be removed and FR-26 rescoped. (Separately: the FHIR `GET` binding check does fire on a cross-patient uuid, but throws an uncaught `patient id invalid` exception surfaced as `500` rather than a clean `403` — binding holds, but this is worth an upstream bug report.) |
| Start/checkpoint assessment draft (FR-30) | None found in OpenEMR core (still true on `master`) | — | FHIR `Questionnaire` and `QuestionnaireResponse` routes are GET-only ([source](https://github.com/openemr/openemr/blob/v8_3_0/apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php#L783-L806)); no Standard API form write route exists in tagged route inventory. | **Resolved — added via module extension (TICK-017, `evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md`).** `POST/GET/PUT /portal/patient/assessment[/:auuid]`, registered by `openemr_modules/aeai-portal-chat` through `OpenEMR\Events\RestApiExtend\RestApiCreateEvent` — no core file modified, the pinned image untouched. Not a database/form-table workaround: it is a new REST route reached through OpenEMR's own first-class module extension point, reusing OpenEMR's own token-derived patient-binding (`HttpRestRequest::getPatientUUIDString()`), proven with a live cross-patient negative test. |
| Complete structured assessment (FR-27) | None found in OpenEMR core (still true on `master`) | — | No `POST`, `PUT`, or `PATCH` route exists for FHIR `QuestionnaireResponse`; the documented patient scope is read/search only ([Swagger](https://github.com/openemr/openemr/blob/v8_3_0/swagger/openemr-api.yaml#L10250-L10259)). | **Resolved — same route as above (TICK-017).** `PUT /portal/patient/assessment/:auuid` with `{"status":"completed"}` validates all required fields (`ONBOARDING_CONTRACT.md` rows 6-9) are present before transitioning status; completed drafts reject further edits (`409`). O-11 is closed. |

## Exact negative checks

The v8.3.0 route tree was searched for the following exact route strings; all were
absent: `PUT /api/patient/:pid/appointment`, `PATCH /api/patient/:pid/appointment`,
`POST|PUT|PATCH /fhir/Appointment`, `/fhir/Schedule`, `/fhir/Slot`, and
`POST|PUT|PATCH /fhir/QuestionnaireResponse`.

This is source evidence, not a claim that an unauthenticated route returns 404. The
release source defines the shipped API surface; the unperformed authenticated probe is
tracked separately in `PROBE_EVIDENCE.md`.

## Deferred operation decisions

TICK-005 and local implementation that uses documented Standard REST operations may
proceed. TICK-018 through TICK-020 must not implement any row still marked as a gap
until a future release, upstream-supported API extension, or approved product-scope
change resolves it. TICK-017's gap rows are resolved this way (an upstream-supported
API extension — OpenEMR's own module/event system, not a workaround; see the two rows
above). Direct MariaDB access, direct `pc_event` updates, native form-table writes, a
parallel scheduler, and delete/recreate emulation remain explicitly rejected by FR-17
and ARCHITECTURE.md §4.
