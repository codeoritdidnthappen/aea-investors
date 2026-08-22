---
id: TICK-048
title: "bug(chat): non-scheduling requests get a scheduling-specific unavailable message"
type: task
epic: EPIC-04
priority: P2
estimate: S
depends_on: [TICK-010, TICK-034]
labels: [chat, groq, bug]
source: [FR-18, FR-19, NFR-19]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/98
builder_commit: c09924d
---
## Context

User report (2026-08-22): asking the AI chat to update an address returns
"Scheduling assistance is unavailable. Please use OpenEMR's native
scheduling screen."

That string is `UNAVAILABLE_RESPONSE`
(`ai_server/llm/groq.py:23-25`). Despite its scheduling-specific wording it
is the **generic** fallback for any unavailability, emitted from two
unrelated places:

- `ai_server/llm/groq.py:285` — `GroqWorkflow.respond` catches
  `GroqUnavailableError`, `ValidationError`, or `httpx.HTTPError` from
  planning and yields it.
- `ai_server/app/chat.py:375` — `ChatService.stream_reply` yields it
  whenever `self.workflow is None` (Groq not configured at all).

So a patient who asks about anything the planner cannot express — an
address change, a records question, a billing question — is told that
*scheduling* is unavailable and pointed at a staff-only OpenEMR screen
they cannot reach from the patient portal. The message is both wrong about
what failed and actionably misleading about what to do next.

This ticket fixes only the message. Actually supporting address updates is
TICK-049 and TICK-050; this stands alone because the misleading text will
still be wrong for every other unsupported request after those land.

## Acceptance Criteria

- [ ] The generic unavailability path no longer claims scheduling
      specifically. It states that the assistant could not handle the
      request and what the patient can do next, without naming a screen
      the patient has no access to.
- [ ] The two call sites are distinguishable: "Groq is not configured"
      (`chat.py:375`, a deployment fault) and "planning failed for this
      turn" (`groq.py:285`, a per-turn fault) do not have to share one
      string if a different message is clearer for each. If they do stay
      shared, that is a deliberate choice recorded in the ticket.
- [ ] A genuinely scheduling-related failure may still say so, but only
      when the failure is actually scheduling-related.
- [ ] Existing tests that assert on the old string
      (`ai_server/tests/test_chat.py:25,251,252`,
      `ai_server/tests/test_groq.py:12,157,168`) are updated, not deleted,
      and still assert the failure path is reached.

## Testing

Unit tests on both call sites: Groq unconfigured, and planning raising
each of `GroqUnavailableError` / `ValidationError` / `httpx.HTTPError`.
Assert the patient-visible text does not mention scheduling for a
non-scheduling turn. CI must be green.

## Out of Scope

Adding any new capability to the planner, and any demographics/address
work (TICK-049, TICK-050). Changing the privacy gate's own
`LOCAL_CORRECTION` message (`ai_server/privacy/gate.py:11`), which is a
separate, correctly-scoped string.
