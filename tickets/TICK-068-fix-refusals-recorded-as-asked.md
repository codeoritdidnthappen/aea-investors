---
id: TICK-068
title: "bug(chat): a refused turn is recorded as the question just asked, so the next turn repeats it and ignores the patient"
type: task
epic: EPIC-09
priority: P1
estimate: S
depends_on: [TICK-063]
labels: [llm, chat, backend, bug]
source: [FR-33, FR-34]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/135
builder_commit: null
---
## Context

Found by live verification of EPIC-09 against a rebuilt local stack (Ollama,
`llama3.1:8b-instruct-q4_K_M`, `LLM_PROVIDER=ollama`), 2026-08-23.

`ModelTurnService` records the outcome of every turn as the question the assistant
has just put to the patient:

    ai_server/app/model_turn.py:363
    state.asked = None if reply == state.pending_prompt else reply

The only reply excluded is the pending-confirmation prompt. Every other reply is
recorded -- including refusals, outage messages and privacy rejections, none of which
are questions. `build_system_prompt` then feeds it back:

    ai_server/llm/prompt.py:177
    system = "\n".join([system, "", f'You have just asked the patient: "{asked}"'])

So the turn after a refusal opens with the model being told that its own refusal text
was the question it asked. The model treats it as the live question and re-emits the
same tool call, discarding what the patient actually typed.

Reproduced twice, in a fresh session each time:

    turn 1  "what are your opening hours?"
            -> ask_general_knowledge("What are the opening hours of the clinic?")
            -> blocked by the privacy gate (see TICK-069), refusal returned
    turn 2  "I would like to book an appointment next week"
            -> ask_general_knowledge("what are the opening hours of the clinic?")
            -> blocked again, same refusal returned

The booking request never reaches `BookingService`. In an earlier session the same
mechanism produced a confidently wrong answer instead of a repeat: the stale
restatement was truncated to "What are the clinic" and Groq returned an encyclopedia
definition of a clinic in response to a request to book an appointment.

The same defect applies to any non-question reply. An outage message, a validation
refusal or a plain informational answer all become `asked`, and the following turn is
prompted as though the patient were answering them.

## Acceptance Criteria

1. `state.asked` is set only when the reply is genuinely a question put to the
   patient. A refusal, an outage message, a privacy rejection and a plain
   informational answer each leave `asked` unset.
2. Given the two-turn sequence above, turn 2 reaches the booking tool. Asserted
   against the tool actually invoked, not against reply text.
3. A turn whose reply is not a question does not cause the next turn's system prompt
   to contain `You have just asked the patient:`.
4. The existing behaviour that AC1 must not break is covered: after the assistant asks
   a real question ("What date suits you?"), a bare "no" is still understood as an
   answer to it.
5. Live verification against a running stack, in the shape of `evidence/TICK-065`:
   the two-turn sequence above, transcript recorded, showing the booking tool reached.

## Out of Scope

The privacy-gate false positive that produces the refusal in the reproduction is
TICK-069. This ticket is the repeat-and-ignore behaviour, which is a defect whatever
caused the first turn to fail.
