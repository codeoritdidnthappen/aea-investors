# Intake backlog

Local Markdown is the source of truth for the AI-assisted OpenEMR onboarding demo. The
ticket files in this directory are authoritative; this file summarises them.

Every section between `<!-- generated:NAME:begin -->` and `<!-- generated:NAME:end -->`
markers is produced from ticket frontmatter and `PRD.md` by
`scripts/verify_backlog_traceability.py --write`. Do not hand-edit inside those markers:
change the ticket, then regenerate. The same script runs in CI without `--write` and fails
when a ticket file is missing from this document, when a generated block is stale, or when
a ticket's `source:` names a requirement ID that `PRD.md` does not declare.

## Epics and execution order

1. **EPIC-01 — Resolve blocking contracts:** TICK-001 through TICK-004 establish the
   pinned OpenEMR integration, portal hook, missing onboarding contract, and Android
   acceptance; TICK-028 later proved a patient-context token end to end. TICK-001 is a
   hard prerequisite for application code.
2. **EPIC-02 — Establish reproducible foundation:** TICK-005 through TICK-007 create
   the pinned project baseline, synthetic evaluation inputs, and artifact gates.
   TICK-052 and TICK-053 are process repairs to the merge gate and to this backlog.
3. **EPIC-03 — Secure runtime boundary:** TICK-008 through TICK-011 implement sessions,
   local privacy rejection, approved model calls, and safe operations. TICK-033,
   TICK-037, and TICK-038 fix scope registration and token issuance found once real
   patient-context calls were made; TICK-055 adds the missing logout.
4. **EPIC-04 — Embed the patient chat:** TICK-012 and TICK-013 implement the supported
   portal integration and accessible streamed chat. Most of this epic is the bug work
   that followed live use of the portal: TICK-032 adds the dashboard entry point,
   TICK-045 through TICK-048 fix unreliable panel load, silently blocked
   iframe-breakout navigation, its duplicated script, and a misleading unavailable
   message, and TICK-051 and TICK-054 stop the chat hijacking the post-login landing
   page and starting an OAuth flow on render. TICK-056 spikes reusing the existing
   portal session instead.
5. **EPIC-05 — Complete confirmed OCR:** TICK-014 through TICK-016 implement local,
   consented OCR, its accuracy gate, and confirmed-only demographic persistence.
   TICK-044 wires the upload into the chat flow; TICK-049 and TICK-050 extend
   confirmed writes to structured addresses.
6. **EPIC-06 — Deliver guided onboarding:** TICK-017 implements the product-approved
   assessment, supportive content, draft checkpoints, and completion. TICK-029 and
   TICK-030 fix distress matching and checkpoint cursor handling; TICK-035 routes real
   chat turns into the flow.
7. **EPIC-07 — Deliver authoritative scheduling:** TICK-018 through TICK-021, plus
   TICK-031, deliver the OpenEMR adapter, anonymous slots, operations, and
   native-policy parity. TICK-020 narrowed to reschedule only and stays
   permanently blocked (no OpenEMR service method exists); TICK-031 split off
   book + cancel-by-status, both buildable. TICK-034, TICK-036, TICK-039, and
   TICK-040 wire booking and cancellation into the chat and portal against real
   appointments; TICK-041 through TICK-043 fix fabricated success, an unreachable
   demographics write, and mononym validation.
8. **EPIC-08 — Deploy and verify:** TICK-022 through TICK-027 establish deployment,
   ingress, desktop/Android coverage, performance measurement, and release gates.

### Ticket roster by epic

Generated from every `tickets/TICK-*.md` frontmatter, so no ticket can be absent from
this document without CI failing.

<!-- generated:roster:begin -->
#### EPIC-01 — Resolve blocking contracts (5 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-001 | spike | P1 | spike(openemr): map required endpoints on a pinned release |
| TICK-002 | spike | P1 | spike(portal): select a supported patient-portal iframe hook |
| TICK-003 | spike | P1 | spike(product): define the intake and supportive-content contract |
| TICK-004 | spike | P1 | spike(mobile): define Android Chrome acceptance |
| TICK-028 | spike | P1 | spike(auth): obtain and prove a patient-context token locally |

#### EPIC-02 — Establish reproducible foundation (5 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-005 | chore | P1 | chore(foundation): create pinned project and CI baseline |
| TICK-006 | feature | P1 | feat(fixtures): generate paired synthetic patient and ID data |
| TICK-007 | chore | P1 | chore(governance): enforce evaluation-data and AI-use release gates |
| TICK-052 | task | P1 | bug(process): merge gate tells agents there is no test suite and not to write tests |
| TICK-053 | task | P2 | chore(tickets): BACKLOG.md traceability stops at TICK-031 and omits every later ticket |

#### EPIC-03 — Secure runtime boundary (8 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-008 | feature | P1 | feat(auth): implement OAuth launch and durable AI sessions |
| TICK-009 | feature | P1 | feat(privacy): enforce local outbound PHI and PII rejection |
| TICK-010 | feature | P1 | feat(llm): integrate approved Groq planning and streaming |
| TICK-011 | feature | P1 | feat(operations): add safe health and observability controls |
| TICK-033 | task | P1 | bug(auth): re-register the AI server's OAuth client with patient-context scopes |
| TICK-037 | task | P1 | bug(auth): custom module-registered scopes never reach the issued access token |
| TICK-038 | task | P1 | bug(onboarding): AI server rejects OpenEMR's own successful assessment-draft response |
| TICK-055 | task | P1 | bug(auth): no logout exists, so portal sign-out leaves a live chat session holding the patient's token |

#### EPIC-04 — Embed the patient chat (10 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-012 | feature | P1 | feat(portal): embed the authenticated chat iframe |
| TICK-013 | feature | P1 | feat(chat): deliver accessible streamed patient conversation |
| TICK-032 | task | P1 | bug(portal): add a dashboard nav tile for the AI Chat entry |
| TICK-045 | task | P1 | bug(portal): AI Chat panel doesn't reliably come up when clicked |
| TICK-046 | task | P2 | task(portal): add fallback when chat iframe-breakout navigation is silently blocked |
| TICK-047 | task | P3 | task(portal): deduplicate OAuth2 iframe-breakout script into shared base template |
| TICK-048 | task | P2 | bug(chat): non-scheduling requests get a scheduling-specific unavailable message |
| TICK-051 | task | P1 | bug(chat): after signing in the patient lands on the full-page chat instead of the dashboard |
| TICK-054 | task | P1 | bug(portal): dashboard render starts an OAuth flow and throws the patient off the page |
| TICK-056 | spike | P2 | spike(auth): determine whether OpenEMR's OAuth2 provider can accept an existing portal session |

#### EPIC-05 — Complete confirmed OCR (6 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-014 | feature | P1 | feat(ocr): process consented synthetic identity uploads locally |
| TICK-015 | task | P1 | task(ocr): gate release on golden-set accuracy |
| TICK-016 | feature | P1 | feat(demographics): persist only confirmed identity fields |
| TICK-044 | feature | P2 | feat(onboarding): wire consented OCR identity upload into the chat flow |
| TICK-049 | feature | P1 | feat(demographics): support an address-only write with structured address columns |
| TICK-050 | feature | P1 | feat(chat): let a patient update their address conversationally, confirm-then-write |

#### EPIC-06 — Deliver guided onboarding (4 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-017 | feature | P1 | feat(onboarding): guide assessment and checkpoint native drafts |
| TICK-029 | task | P1 | task(onboarding): normalize curly apostrophes in distress-phrase matching |
| TICK-030 | task | P2 | task(onboarding): fold checkpoint_field's stale-cursor error into its documented contract |
| TICK-035 | feature | P1 | feat(chat): route chat turns into the guided onboarding flow |

#### EPIC-07 — Deliver authoritative scheduling (12 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-018 | feature | P1 | feat(openemr): implement authoritative scheduling adapter |
| TICK-019 | feature | P1 | feat(scheduling): expose genuine open slots as anonymous tokens |
| TICK-020 | feature | P1 | feat(scheduling): reschedule an appointment through OpenEMR |
| TICK-021 | task | P1 | task(scheduling): verify native policy parity |
| TICK-031 | feature | P1 | feat(scheduling): book and cancel appointments through OpenEMR |
| TICK-034 | feature | P1 | feat(chat): replace the no-op tool with real appointment booking |
| TICK-036 | feature | P2 | feat(chat): anonymously reference and cancel a real appointment |
| TICK-039 | task | P1 | bug(chat): cancellation intent never selects a real, available appointment |
| TICK-040 | task | P1 | bug(scheduling): booking's write route is structurally unreachable for a genuine patient token |
| TICK-041 | task | P0 | bug(chat): final-response model call can fabricate a false success on a real OpenEMR failure |
| TICK-042 | task | P0 | bug(onboarding): demographics write route is structurally unreachable for a genuine patient token |
| TICK-043 | task | P2 | bug(onboarding): a confirmed mononym (empty family name) can never even be entered |

#### EPIC-08 — Deploy and verify (7 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-022 | chore | P1 | chore(local): create reproducible local demo topology |
| TICK-023 | chore | P1 | chore(local): configure local Caddy ingress |
| TICK-024 | task | P1 | task(verification): run desktop Chrome critical-flow coverage |
| TICK-025 | task | P1 | task(verification): validate approved Android Chrome behavior |
| TICK-026 | task | P2 | task(performance): measure chat and scheduling targets |
| TICK-027 | task | P1 | task(local): verify privacy and local-demo readiness |
| TICK-057 | task | P1 | bug(deploy): the chat never renders -- the stack mounts a deleted build worktree and cannot be restarted |

#### EPIC-09 — Replace hand-coded intent with a local model (12 tickets)

| Ticket | Type | Priority | Title |
|---|---|---|---|
| TICK-058 | feature | P1 | feat(llm): dispatch on LLM_PROVIDER and add an OpenAI-compatible local client |
| TICK-059 | task | P1 | chore(deploy): run Ollama in the local topology and pin the model |
| TICK-060 | feature | P1 | feat(chat): define the schema-constrained tool surface the model may call |
| TICK-061 | feature | P1 | feat(chat): validate every model-proposed field before it can reach the record |
| TICK-062 | feature | P1 | feat(eval): build the acceptance corpus and harness, and pick the model with it |
| TICK-063 | feature | P1 | feat(chat): route every turn through the local model and execute its tool call |
| TICK-064 | feature | P1 | feat(privacy): narrow Groq to restated general knowledge and stop forwarding patient text |
| TICK-065 | task | P1 | refactor(chat): delete the deterministic intent handlers and report honestly when the model is down |
| TICK-066 | feature | P2 | feat(deploy): add the vLLM backend and prove it agrees with the development one |
| TICK-067 | spike | P2 | spike(llm): establish how the model handles the turns no capability covers |
| TICK-068 | task | P1 | bug(chat): a refused turn is recorded as the question just asked, so the next turn repeats it and ignores the patient |
| TICK-069 | task | P1 | bug(privacy): the gate treats any Presidio entity as PHI, so ordinary questions are refused and the patient is told they sent personal information |
<!-- generated:roster:end -->

## Requirement traceability

Generated from each ticket's `source:` field against the requirement IDs declared in
`PRD.md`. A requirement with no covering ticket is listed with `_no ticket_` rather than
omitted, and tickets that trace to no requirement are named beneath the table instead of
being silently dropped.

<!-- generated:traceability:begin -->
| Requirement | Tickets |
|---|---|
| FR-1 | TICK-002, TICK-008, TICK-012, TICK-024, TICK-032, TICK-045, TICK-055 |
| FR-2 | TICK-002, TICK-012, TICK-024, TICK-032, TICK-045, TICK-046, TICK-047, TICK-051, TICK-054, TICK-056, TICK-057 |
| FR-3 | TICK-001, TICK-002, TICK-008, TICK-012, TICK-028, TICK-033, TICK-056 |
| FR-4 | TICK-002, TICK-012, TICK-013 |
| FR-5 | TICK-003, TICK-017, TICK-035, TICK-037 |
| FR-6 | TICK-014, TICK-016, TICK-024, TICK-035, TICK-042, TICK-043, TICK-044, TICK-049, TICK-050 |
| FR-7 | TICK-014, TICK-035, TICK-044 |
| FR-8 | TICK-003, TICK-017, TICK-029, TICK-035, TICK-037, TICK-038 |
| FR-9 | TICK-001, TICK-018, TICK-036, TICK-039 |
| FR-10 | TICK-001, TICK-018, TICK-019 |
| FR-11 | TICK-019, TICK-034, TICK-040 |
| FR-12 | TICK-001, TICK-024, TICK-031, TICK-034, TICK-040 |
| FR-13 | TICK-001, TICK-020, TICK-040 |
| FR-14 | TICK-001, TICK-024, TICK-031, TICK-036, TICK-039, TICK-041 |
| FR-15 | TICK-018, TICK-036, TICK-039, TICK-041 |
| FR-16 | TICK-031, TICK-034, TICK-036, TICK-039, TICK-041 |
| FR-17 | TICK-001, TICK-016, TICK-018, TICK-042, TICK-043, TICK-049, TICK-050 |
| FR-18 | TICK-010, TICK-013, TICK-024, TICK-048 |
| FR-19 | TICK-013, TICK-024, TICK-048 |
| FR-20 | TICK-010, TICK-018, TICK-019, TICK-020, TICK-031, TICK-034, TICK-036, TICK-039, TICK-040, TICK-041 |
| FR-21 | TICK-014, TICK-044 |
| FR-22 | TICK-014, TICK-044 |
| FR-23 | TICK-014, TICK-044 |
| FR-24 | TICK-006 |
| FR-25 | TICK-006, TICK-014, TICK-044 |
| FR-26 | TICK-001, TICK-016, TICK-028, TICK-042, TICK-043, TICK-049, TICK-050 |
| FR-27 | TICK-001, TICK-003, TICK-017, TICK-035, TICK-037, TICK-038 |
| FR-28 | TICK-018, TICK-020, TICK-021, TICK-031, TICK-040 |
| FR-29 | TICK-010 |
| FR-30 | TICK-001, TICK-017, TICK-030, TICK-035 |
| FR-31 | TICK-051 |
| FR-32 | TICK-054 |
| FR-33 | TICK-058, TICK-059, TICK-060, TICK-062, TICK-063, TICK-065, TICK-066, TICK-067, TICK-068 |
| FR-34 | TICK-064, TICK-068, TICK-069 |
| FR-35 | TICK-060, TICK-061, TICK-063 |
| NFR-1 | TICK-006, TICK-027 |
| NFR-2 | TICK-009, TICK-010, TICK-017, TICK-050 |
| NFR-3 | TICK-009, TICK-017, TICK-029 |
| NFR-4 | TICK-009, TICK-017 |
| NFR-5 | TICK-009, TICK-010, TICK-019 |
| NFR-6 | TICK-008, TICK-012 |
| NFR-7 | TICK-008, TICK-012, TICK-013 |
| NFR-8 | TICK-009, TICK-011, TICK-027 |
| NFR-9 | TICK-022, TICK-023 |
| NFR-10 | TICK-008 |
| NFR-11 | TICK-020, TICK-021, TICK-031, TICK-041 |
| NFR-12 | TICK-018, TICK-019, TICK-021 |
| NFR-13 | TICK-026 |
| NFR-14 | TICK-026 |
| NFR-15 | TICK-001, TICK-022, TICK-057 |
| NFR-16 | TICK-022, TICK-023 |
| NFR-17 | TICK-023 |
| NFR-18 | TICK-005, TICK-024, TICK-027, TICK-052 |
| NFR-19 | TICK-004, TICK-013, TICK-024, TICK-025, TICK-045, TICK-046, TICK-048, TICK-050 |
| NFR-20 | TICK-022, TICK-027 |
| NFR-21 | TICK-005, TICK-007, TICK-027 |
| NFR-22 | TICK-011, TICK-027 |
| NFR-23 | TICK-014, TICK-027, TICK-044 |
| NFR-24 | TICK-006, TICK-007, TICK-027 |
| NFR-25 | TICK-001, TICK-016, TICK-018, TICK-028, TICK-033, TICK-037, TICK-042, TICK-049 |
| NFR-26 | TICK-010, TICK-027 |
| NFR-27 | TICK-009 |
| NFR-28 | TICK-006, TICK-009, TICK-027 |
| NFR-29 | TICK-006, TICK-014, TICK-015, TICK-027, TICK-044 |
| NFR-30 | TICK-008, TICK-028 |
| NFR-31 | TICK-008, TICK-022, TICK-055 |
| NFR-32 | TICK-008 |
| NFR-33 | TICK-008, TICK-017 |
| NFR-34 | TICK-023, TICK-027 |
| NFR-35 | TICK-004, TICK-024, TICK-025, TICK-027 |
| NFR-36 | TICK-061, TICK-062, TICK-066 |

Tickets with an empty `source:` (backlog hygiene, governed by no requirement): TICK-053.
<!-- generated:traceability:end -->

## Assumptions and decisions

- The user approved definition tickets for the missing onboarding brief and supportive-content mapping; TICK-003 blocks TICK-017.
- The user approved discovery spikes for unresolved OpenEMR endpoints and portal hook; TICK-001 blocks application code.
- Android Chrome is the only mobile target. TICK-004 records exact allowable degradation before implementation and TICK-025 verifies it.
- Testing uses Definition of Done in each ticket, not separate QA tickets or TDD-only sequencing.
- Local artifacts are intended for a fresh documentation branch and merge request after GitLab connectivity/authentication is restored.

## Deferred items

No source requirement is deferred. NFR-13 and NFR-14 are P2 because the PRD labels them should-have; all other requirements are P1.

## Audit summary

The original generation run left this section describing an audit that had not happened.
It is now a mechanical check: `scripts/verify_backlog_traceability.py` runs in CI beside
`scripts/verify_release_governance.py`, so backlog coverage is verified on every push
rather than reviewed once. Current state:

<!-- generated:audit:begin -->
- Tickets in `tickets/`: 69 across 9 epics; tickets tracing to no requirement: 1.
- Requirements declared in `PRD.md`: 71; covered by at least one ticket: 71.
- Requirements with no covering ticket: none.
- Every ticket ID above is present because `scripts/verify_backlog_traceability.py` fails CI otherwise; the audit is continuous, not a one-off review.
<!-- generated:audit:end -->
