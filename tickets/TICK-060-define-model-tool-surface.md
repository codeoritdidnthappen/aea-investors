---
id: TICK-060
title: "feat(chat): define the schema-constrained tool surface the model may call"
type: feature
epic: EPIC-09
priority: P1
estimate: M
depends_on: [TICK-058]
labels: [llm, chat, backend]
source: [FR-33, FR-35]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/125
builder_commit: null
---
## Context

`docs/LOCAL_LLM_SPEC.md` D6, D9 and the draft tool surface. The model never acts.
It emits exactly one tool call under a strict schema; application code executes it.
This ticket defines that contract.

Every tool maps to a service that already exists, so this is a re-facing of current
capability rather than new function:

| Tool | Backed by | Writes? |
|---|---|---|
| `update_address` | `PatientDemographicsUpdateService` | yes |
| `update_demographics` | `PatientDemographicsUpdateService` | yes |
| `record_assessment_answer` | `AssessmentDraftService` | yes |
| `list_appointments` | `AppointmentDiscoveryService` | no |
| `find_slots` | `SlotDiscoveryService` | no |
| `book_appointment` | `BookingService` | yes |
| `cancel_appointment` | `CancellationService` | yes |
| `extract_document_fields` | `OcrService` | no |
| `ask_general_knowledge` | Groq, via restatement | no |
| `reply` | none | no |

`RescheduleService` is deliberately absent: TICK-020 established that no OpenEMR
service method exists for it, and exposing a tool the system cannot honour invites
the model to promise it.

## Acceptance Criteria

- [ ] Each tool has a strict schema. Where the runtime supports constrained
      decoding, the schema constrains generation so a malformed call cannot be
      produced; where it does not, a malformed call is rejected before execution
      and never partially applied.
- [ ] Exactly one tool call per turn. A response containing none, or more than one,
      is a defined error with defined patient-visible behaviour -- not an exception
      that reaches the transcript.
- [ ] Every tool declares whether it writes. Writing tools route through TICK-061's
      validation and the confirmation step without exception; the schema alone is
      never sufficient authority to write.
- [ ] A tool name the model invents is refused and reported, not silently ignored.
      Silence would make a hallucinated capability look like a no-op.
- [ ] Tool arguments carry no OpenEMR identifiers the model could fabricate.
      Appointment and slot references use the existing anonymous token scheme, so a
      hallucinated identifier cannot address a real record.
- [ ] The surface is documented where it is defined, including why reschedule is
      absent.

## Testing

Unit tests per tool: a well-formed call executes, a malformed call is refused
without side effects, an unknown tool name is refused, and a fabricated identifier
cannot reach a service. Assert no writing tool can execute without passing
validation. CI must be green.

## Out of Scope

Field-level validation (TICK-061). Routing turns to the model (TICK-063). Prompt
design and the eval corpus (TICK-062).
