---
id: TICK-050
title: "feat(chat): let a patient update their address conversationally, confirm-then-write"
type: feature
epic: EPIC-05
priority: P1
estimate: L
depends_on: [TICK-049, TICK-035, TICK-042]
labels: [chat, demographics, privacy, backend]
source: [FR-6, FR-17, FR-26, NFR-2, NFR-19]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/100
builder_commit: 635bbe3
---
## Context

User request (2026-08-22): a patient should be able to update their address
in the AI chat — be prompted for it, see it echoed back parsed, and have
the chat itself write it to OpenEMR. Today they get
"Scheduling assistance is unavailable..." instead (the misleading wording
itself is TICK-048).

### Why this cannot be a Groq tool/intent

`PlanningOutput` (`ai_server/llm/groq.py:60-67`) is
`intent: Literal["information", "book", "reschedule", "cancel"]` with
`extra="forbid"`, injected as a Groq **strict** JSON schema (`:179-188`).
The planner is structurally incapable of expressing an address update.
`ToolFactory` (`ai_server/app/chat.py:347`) also returns a *single* tool
per turn, and `BookingTool.execute` (`:139-150`) is the entire dispatch
table, falling through to `NoActionTool`.

More importantly, **the address must never reach Groq**. NFR-2 forbids
sending patient information to an external LLM, and the Presidio gate
(`ai_server/privacy/gate.py:186-188`) will flag an address and return
`LOCAL_CORRECTION` (`gate.py:11`) before planning even runs. Routing an
address through the planner would either be blocked by the gate or, if it
slipped through, would violate NFR-2.

The established precedent is therefore the right one: onboarding is a
**deterministic local path that never calls Groq**, routed *before* the
Groq chat service in `POST /api/chat`
(`ai_server/app/main.py:197-213`, via `onboarding_mode`,
`ai_server/app/onboarding_chat.py:163-172`) rather than registered as an
`AuthoritativeTool` — see TICK-035, lines 36-40. Address update should be
routed the same way.

### Why onboarding cannot simply be reused

`onboarding_mode` only triggers on the phrases at
`onboarding_chat.py:113-126` ("start onboarding", "start intake", ...) —
none mention address or demographics. Once entered, all eight fields must
be answered in order (`:61-72`), identity fields included, so there is no
way to answer only the address. `checkpoint_field`
(`ai_server/onboarding/flow.py:161-165`) additionally refuses identity
fields outright.

### What can be reused

- The address prompt and its JSON shape (`onboarding_chat.py:106-110`).
- `Address` + `validate_address` (`ai_server/onboarding/fields.py:155-162`,
  `:194-232`) — real state-code and ZIP validation, entirely local.
- The review-then-CONFIRM gate: `_review_summary` (`:252-261`) and
  `_handle_confirmation` (`:455-485`), where anything that is not an
  explicit confirmation re-shows the review and writes nothing (`:465-468`).
- The address-only structured write delivered by TICK-049.

Note the general chat is stateless per turn — there is no pending-write
state in `ai_server/app/chat.py` today, so this flow needs its own
short-lived session state, as onboarding has (`_SessionState`,
`onboarding_chat.py:264-277`).

## Acceptance Criteria

- [ ] A patient can start an address update from the chat with a plain
      request (e.g. "update my address", "I moved", "change my address"),
      and is prompted for the address. The trigger does not collide with
      the onboarding start phrases or with scheduling requests.
- [ ] The address the patient supplies is parsed and **echoed back in a
      structured, human-readable form** (street, street2 if given, city,
      state, ZIP) for review before anything is written.
- [ ] Nothing is written until the patient explicitly confirms. Any reply
      that is not a confirmation re-shows the parsed address and writes
      nothing — matching TICK-016's confirmed-only rule.
- [ ] On confirmation the chat writes the address itself via TICK-049's
      address-only structured path, and reports the real outcome. A failed
      write is reported as a failure and never described as success
      (the TICK-041 rule).
- [ ] An invalid address (bad state code, malformed ZIP, missing street or
      city) is rejected locally with a specific correction prompt, and the
      patient can correct it without restarting the flow.
- [ ] **The address is never sent to Groq.** Proven by test and by
      inspecting outbound payloads: the whole flow runs locally, ahead of
      the Groq path, exactly as onboarding does.
- [ ] The patient can abandon the flow mid-way and return to normal chat
      without a partial write.
- [ ] A patient with an in-progress onboarding session is not hijacked
      into this flow, and vice versa — the two routes are unambiguous.
- [ ] Meets the accessibility bar the embedded chat is already held to
      (NFR-19).

## Testing

Turn-sequence tests in the style of
`ai_server/tests/test_onboarding_chat.py`: trigger → prompt → invalid
address → correction → parsed echo-back → non-confirmation re-shows review
→ confirmation writes exactly once. Assert against a synthetic OpenEMR
that the write happened with structured components and that name/DOB were
untouched. Include an explicit assertion that no outbound Groq payload
ever contains the address. Verify live against the local Docker stack with
a real patient session, not only against stubs. CI must be green.

## Out of Scope

Editing name or date of birth from the chat (TICK-016's Out of Scope still
applies, and TICK-043 settled mononyms). Adding an address-update intent to
`PlanningOutput` — deliberately rejected above on NFR-2 grounds. The
misleading unavailable message (TICK-048). Persisting this flow's session
state across an ai-server restart; like onboarding identity
(`onboarding_chat.py:14-19`), in-memory is acceptable for the demo.

## Open question for product

The PRD has no requirement covering a patient updating demographics
conversationally outside the OCR/onboarding flow. FR-26 mandates the
confirm-then-write rule but is scoped to values extracted from an uploaded
identity document (FR-6, FR-25). This ticket is filed against FR-26's
*rule* rather than its scope; the PRD likely needs an addendum so the
requirement register and traceability table stay honest.
