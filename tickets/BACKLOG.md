# Intake backlog

Local Markdown is the source of truth for the AI-assisted OpenEMR onboarding demo. This
backlog has 8 epics and 27 tickets: 13 features, 6 tasks, 4 chores, and 4 spikes.

## Epics and execution order

1. **EPIC-01 — Resolve blocking contracts:** TICK-001 through TICK-004 establish the
   pinned OpenEMR integration, portal hook, missing onboarding contract, and Android
   acceptance. TICK-001 is a hard prerequisite for application code.
2. **EPIC-02 — Establish reproducible foundation:** TICK-005 through TICK-007 create
   the pinned project baseline, synthetic evaluation inputs, and artifact gates.
3. **EPIC-03 — Secure runtime boundary:** TICK-008 through TICK-011 implement sessions,
   local privacy rejection, approved model calls, and safe operations.
4. **EPIC-04 — Embed the patient chat:** TICK-012 and TICK-013 implement the supported
   portal integration and accessible streamed chat.
5. **EPIC-05 — Complete confirmed OCR:** TICK-014 through TICK-016 implement local,
   consented OCR, its accuracy gate, and confirmed-only demographic persistence.
6. **EPIC-06 — Deliver guided onboarding:** TICK-017 implements the product-approved
   assessment, supportive content, draft checkpoints, and completion.
7. **EPIC-07 — Deliver authoritative scheduling:** TICK-018 through TICK-021 deliver
   the OpenEMR adapter, anonymous slots, operations, and native-policy parity.
8. **EPIC-08 — Deploy and verify:** TICK-022 through TICK-027 establish deployment,
   ingress, desktop/Android coverage, performance measurement, and release gates.

## Requirement traceability

| Requirement | Tickets |
|---|---|
| FR-1 | TICK-002, TICK-008, TICK-012, TICK-024 |
| FR-2 | TICK-002, TICK-012, TICK-024 |
| FR-3 | TICK-001, TICK-002, TICK-008, TICK-012 |
| FR-4 | TICK-002, TICK-012, TICK-013 |
| FR-5 | TICK-003, TICK-017 |
| FR-6 | TICK-014, TICK-016, TICK-024 |
| FR-7 | TICK-014 |
| FR-8 | TICK-003, TICK-017 |
| FR-9 | TICK-001, TICK-018 |
| FR-10 | TICK-001, TICK-018, TICK-019 |
| FR-11 | TICK-019 |
| FR-12 | TICK-001, TICK-020, TICK-024 |
| FR-13 | TICK-001, TICK-020, TICK-024 |
| FR-14 | TICK-001, TICK-020, TICK-024 |
| FR-15 | TICK-018 |
| FR-16 | TICK-020 |
| FR-17 | TICK-001, TICK-016, TICK-018 |
| FR-18 | TICK-010, TICK-013, TICK-024 |
| FR-19 | TICK-013, TICK-024 |
| FR-20 | TICK-010, TICK-018, TICK-019, TICK-020 |
| FR-21 | TICK-014 |
| FR-22 | TICK-014 |
| FR-23 | TICK-014 |
| FR-24 | TICK-006 |
| FR-25 | TICK-006, TICK-014 |
| FR-26 | TICK-001, TICK-016 |
| FR-27 | TICK-001, TICK-003, TICK-017 |
| FR-28 | TICK-018, TICK-020, TICK-021 |
| FR-29 | TICK-010 |
| FR-30 | TICK-001, TICK-017 |
| NFR-1 | TICK-006, TICK-027 |
| NFR-2 | TICK-009, TICK-010, TICK-017 |
| NFR-3 | TICK-009, TICK-017 |
| NFR-4 | TICK-009, TICK-017 |
| NFR-5 | TICK-009, TICK-010, TICK-019 |
| NFR-6 | TICK-008, TICK-012 |
| NFR-7 | TICK-008, TICK-012, TICK-013 |
| NFR-8 | TICK-009, TICK-011, TICK-027 |
| NFR-9 | TICK-022, TICK-023 |
| NFR-10 | TICK-008 |
| NFR-11 | TICK-020, TICK-021 |
| NFR-12 | TICK-018, TICK-019, TICK-021 |
| NFR-13 | TICK-026 |
| NFR-14 | TICK-026 |
| NFR-15 | TICK-001, TICK-022 |
| NFR-16 | TICK-022, TICK-023 |
| NFR-17 | TICK-023 |
| NFR-18 | TICK-005, TICK-024, TICK-027 |
| NFR-19 | TICK-004, TICK-013, TICK-024, TICK-025 |
| NFR-20 | TICK-022, TICK-027 |
| NFR-21 | TICK-005, TICK-007, TICK-027 |
| NFR-22 | TICK-011, TICK-027 |
| NFR-23 | TICK-014, TICK-027 |
| NFR-24 | TICK-006, TICK-007, TICK-027 |
| NFR-25 | TICK-001, TICK-016, TICK-018 |
| NFR-26 | TICK-010, TICK-027 |
| NFR-27 | TICK-009 |
| NFR-28 | TICK-006, TICK-009, TICK-027 |
| NFR-29 | TICK-006, TICK-014, TICK-015, TICK-027 |
| NFR-30 | TICK-008 |
| NFR-31 | TICK-008, TICK-022 |
| NFR-32 | TICK-008 |
| NFR-33 | TICK-008, TICK-017 |
| NFR-34 | TICK-023, TICK-027 |
| NFR-35 | TICK-004, TICK-024, TICK-025, TICK-027 |

## Assumptions and decisions

- The user approved definition tickets for the missing onboarding brief and supportive-content mapping; TICK-003 blocks TICK-017.
- The user approved discovery spikes for unresolved OpenEMR endpoints and portal hook; TICK-001 blocks application code.
- Android Chrome is the only mobile target. TICK-004 records exact allowable degradation before implementation and TICK-025 verifies it.
- Testing uses Definition of Done in each ticket, not separate QA tickets or TDD-only sequencing.
- Local artifacts are intended for a fresh documentation branch and merge request after GitLab connectivity/authentication is restored.

## Deferred items

No source requirement is deferred. NFR-13 and NFR-14 are P2 because the PRD labels them should-have; all other requirements are P1.

## Audit summary

Pending independent validation after generation: frontmatter, required sections, sequential IDs, traceability, and dependency graph will be checked before publication.
