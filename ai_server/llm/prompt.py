"""The one tool-call prompt, and the rendering of a single turn into messages.

TICK-063. This text and this renderer used to live in
`scripts/evaluate_acceptance_corpus.py`, which is the only place the prompt has ever
been *measured* (`docs/LOCAL_LLM_SPEC.md` D8, D17: the model was chosen on the corpus,
not on reputation). Now that the model is the front door for every turn (D9), the
runtime needs the same prompt -- and `deploy/local/ai-server.Dockerfile` copies
`ai_server/` and nothing else, so the harness cannot be the owner of a string the
deployment depends on.

Moving it here rather than copying it is the whole point: the harness imports from this
module, so the prompt the corpus scores and the prompt production sends are the same
object, not two strings that agree today. `PROMPT_VERSION` is recorded in `AI_USAGE.md`
(NFR-21) and pins every recorded replay -- `load_replay` refuses a replay recorded under
a different version, which is what stops a prompt edit from riding on a run that
predates it.

**What the renderer adds per turn, and why none of it is a phrasing rule.** The system
message carries the tokens this session was actually issued, today's date, the question
the assistant last asked, any change waiting for confirmation, and any document the
patient just uploaded. Every one of those is state this application recorded when it
happened (`ai_server/app/conversation.py`), handed to the model as context. None of it
is derived by matching the patient's words against a pattern, and none of it decides the
turn: the model reads it and chooses (D9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ai_server.scheduling.appointments import AnonymousAppointmentToken
from ai_server.scheduling.slots import AnonymousSlotToken

# Bumping this invalidates every recorded replay (see `load_replay` in
# `scripts/evaluate_acceptance_corpus.py`), which is what stops a prompt edit from
# quietly riding on a run that predates it. Recorded in AI_USAGE.md per NFR-21.
PROMPT_VERSION = "acceptance-tool-call-v1"

SYSTEM_PROMPT = """\
You are the assistant inside a mental-health clinic's patient portal. Each turn you \
emit exactly one tool call as a single JSON object and nothing else -- no prose, no \
explanation, no markdown fence.

The object has exactly two keys: "tool", naming one of the tools below, and \
"arguments", an object holding only that tool's own arguments.

Tools that change the patient's record:
- update_address {street1, street2 (optional), city, state, zip_code} -- a mailing \
address. "state" is the two-letter US code. "street1" is the house number and street \
and nothing else. An apartment, suite, floor or unit goes in "street2" on its own -- \
never appended to "street1".
- update_demographics {given_name, family_name, date_of_birth} -- include only the \
fields the patient is actually changing; omit the rest. "date_of_birth" is YYYY-MM-DD.
- record_assessment_answer {field, answer} -- one intake answer. "field" is one of \
preferred_contact, help_type, visit_preference, accommodations. "answer" must be a \
value from that field's list below, exactly as written there.
- book_appointment {slot_token} -- a slot token from the times offered in this \
conversation.
- cancel_appointment {appointment_token} -- an appointment token from the patient's \
own appointments listed in this conversation.

Tools that change nothing:
- list_appointments {} -- the patient's upcoming appointments.
- find_slots {} -- the times currently open.
- extract_document_fields {upload_id} -- read a document the patient uploaded.
- ask_general_knowledge {restatement} -- a general question that carries no detail \
about this patient. Rewrite it as a standalone question in your own words.
- reply {message} -- answer the patient directly. Use this whenever no other tool fits.

Values for record_assessment_answer:
- preferred_contact: "portal_message", or "email <address>", or "phone <number in \
+1XXXXXXXXXX form>".
- help_type: counseling_or_therapy, psychiatric_evaluation_or_medication_support, \
both, not_sure_yet.
- visit_preference: a format and a time window joined by a comma, e.g. \
"video,weekday_morning". Formats: in_person, video, either, not_sure. Time windows: \
weekday_morning, weekday_afternoon, weekday_evening, weekend, no_preference.
- accommodations: "none", or one or more of language_interpreter, \
hearing_accommodation, vision_accommodation, mobility_accommodation, \
other_accommodation, joined by commas.

Rules that matter more than being helpful:

1. Never invent a value. Write down only what the patient actually said. If a field is \
needed and the patient did not give it, do not guess it and do not fill it from \
context -- use "reply" and ask them for it.
2. A partial answer is not an answer. If the patient gives a street but no city, state \
or ZIP, use "reply" and ask for the rest. Do not send update_address.
3. If the patient corrects themselves mid-sentence, use the corrected value, not the \
one they replaced.
4. Ignore lead-in words. "Update it to: 12 Oak Street" means the street is "12 Oak \
Street" -- the lead-in is not part of the value.
5. If the patient asks a question, declines to answer, or answers a different question \
than the one you asked, do not record anything. Use "reply", or \
"ask_general_knowledge" if it is a general question you could answer -- prefer that \
over answering from your own knowledge in "reply".
6. record_assessment_answer records an answer to the question you actually asked. If \
the patient tells you something else instead -- however useful it sounds -- do not file \
it against the pending question and do not file it against another one. Use "reply". If \
they do answer the question you asked, including answering "no" or "I don't know", \
record it rather than replying.
7. Only ever use a slot_token or appointment_token listed above. Never make one up and \
never substitute a nearby one. If the patient asks about a time that was not offered, \
an appointment not in that list, or anyone other than themselves -- a relative, a \
friend, a child -- use "reply". Cancelling the wrong appointment is not recoverable by \
the patient.

A refusal to act costs the patient one more message. A wrong value written into their \
record may never be noticed. When those two trade off, choose the refusal.\
"""

# How a change awaiting the patient's approval is described to the model (TICK-063 AC5).
# The model is told what it read back and what each kind of answer means; it is never
# told to look for a word. "Reply CONFIRM" is what `validation._CONFIRM_INSTRUCTION`
# invites, but the patient who instead says "actually, make it 2004" has to be
# understood too, and only the model can tell those apart -- a cancel keyword or a
# confirmation phrase list is the mechanism this ticket exists to remove.
_PENDING_CONFIRMATION_INSTRUCTION = """\
A change is waiting for this patient's approval. You read it back to them as:
{prompt}

If this message agrees to it, emit that same tool call again with exactly the same \
arguments. If it corrects any part of it, emit that same tool call with the corrected \
arguments and only the corrected arguments -- keeping every part they did not correct. \
If it does neither, use whichever tool actually fits; the pending change is then \
abandoned and nothing is saved.\
"""

_UPLOAD_INSTRUCTION = (
    'The patient has just uploaded a document. Its upload_id is "{upload_id}". Use '
    "extract_document_fields to read it before asking them to confirm anything from it."
)


def render_turn_messages(
    *,
    message: str,
    now: datetime,
    offered_slots: Sequence[AnonymousSlotToken] = (),
    offered_appointments: Sequence[AnonymousAppointmentToken] = (),
    asked: str | None = None,
    pending_confirmation: str | None = None,
    upload_id: str | None = None,
    transcript: Sequence[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """Build the chat-completions messages for exactly one turn.

    The offered slots and appointments are rendered into the system message with their
    tokens, because a model asked to book something can only use a token it was given --
    and a model that invents one instead is exactly what rule 7 of the prompt and
    `validate_offered_slot` are both there to catch.

    With no transcript, no pending confirmation and no upload -- which is every
    acceptance-corpus case -- this returns exactly the system+user pair the corpus was
    measured on, so `scripts/evaluate_acceptance_corpus.render_messages` is this
    function rather than a second copy of it.
    """
    offered = [
        "Times offered to this patient in this conversation:",
        *(
            f"- {_window(slot.starts_at, slot.ends_at)} -> slot_token {slot.slot_token}"
            for slot in offered_slots
        ),
        "",
        "This patient's own upcoming appointments:",
        *(
            f"- {_window(appointment.starts_at, appointment.ends_at)} -> "
            f"appointment_token {appointment.appointment_token}"
            for appointment in offered_appointments
        ),
        "",
        f"Today is {now:%A %d %B %Y}.",
    ]
    system = "\n".join([SYSTEM_PROMPT, "", *offered])
    if asked:
        system = "\n".join([system, "", f'You have just asked the patient: "{asked}"'])
    if pending_confirmation:
        system = "\n".join(
            [system, "", _PENDING_CONFIRMATION_INSTRUCTION.format(prompt=pending_confirmation)]
        )
    if upload_id:
        system = "\n".join([system, "", _UPLOAD_INSTRUCTION.format(upload_id=upload_id)])
    messages = [{"role": "system", "content": system}]
    messages.extend({"role": role, "content": text} for role, text in transcript)
    messages.append({"role": "user", "content": message})
    return messages


def _window(starts_at: datetime, ends_at: datetime) -> str:
    return f"{starts_at:%A %d %B %Y}, {starts_at:%H:%M} to {ends_at:%H:%M}"
