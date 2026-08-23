"""The only door between a model-proposed value and a patient's record.

TICK-061, from `docs/LOCAL_LLM_SPEC.md` D6 ("model proposes, code disposes") and D15
("any field reaching the record is right or refused"). TICK-060 published the tool
surface and bounded the *shape* of a call; this module judges the *content* of every
field of every writing tool, and is the only place a `WriteAuthority` is minted.

**Why it is independent.** This module never reads `call.arguments` -- it is handed a
plain mapping and the field rules it applies live in `ai_server.onboarding.fields`,
which knows nothing about models, tools, or prompts. The rules therefore hold when the
model is wrong, when a prompt regresses, when the runtime is swapped from Ollama to
vLLM (D7), and they would have held for the regex parser too: TICK-050 wrote
`"Update it to: 2002 Bridge Avenue"` into a chart precisely because its validator
accepted whatever it was given and its confirmation step rendered whatever its parser
produced. A safety net that mirrors its input is not a safety net.

**Refuse, never repair.** A field that cannot be validated raises `ValidationRefused`;
nothing is written and nothing is guessed at. Under PRD NFR-36 a refusal is an
acceptable outcome and a wrong write is not, so where the two trade off this module
chooses the refusal, and the patient is always told what to send instead.

**The confirmation is a second check, not an echo.** `confirmation_prompt()` accepts
only a `ValidatedWrite`, which cannot exist unless validation passed, and it re-runs
the validators over those values before rendering a single one of them. The model's
proposed values are structurally unable to reach a confirmation prompt: they are not in
the object the renderer is given. `write_authority()` validates a third time and mints
its `WriteAuthority` from that fresh result, so what `executable_arguments()` hands a
backing service is the validator's output rather than anything the model said.

**Three passes, one authority.** The repetition is deliberate and cheap: validation,
confirmation, and authorisation are the three moments where a wrong value would become
a wrong record, and each re-derives its values from the same rules rather than trusting
the step before it. Because every validator is normalising and idempotent, a value that
survives the first pass survives all three unchanged -- a value that does not is
refused at whichever pass first notices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ai_server.llm.tools import WRITING_TOOLS, ToolSurfaceError, WriteAuthority
from ai_server.onboarding.fields import (
    ACCOMMODATIONS,
    CONTACT_METHODS,
    DRAFT_FIELDS,
    VISIT_FORMATS,
    VISIT_TIME_WINDOWS,
    FieldValidationError,
    validate_accommodations,
    validate_address,
    validate_date_of_birth,
    validate_help_type,
    validate_preferred_contact,
    validate_text_name,
    validate_visit_preference,
)
from ai_server.scheduling.appointments import AnonymousAppointmentToken
from ai_server.scheduling.slots import AnonymousSlotToken

logger = logging.getLogger(__name__)

# Patient-visible refusals (AC5). Each says what was wrong and what to send instead. No
# refusal quotes the model's output or names a field, a schema, or a tool: the patient
# asked for a change in their own words and gets an answer in theirs.
STREET_REFUSAL = (
    "I could not read that as a street address, so nothing was saved. Please send just "
    'the street line by itself -- for example "100 Maple Ave" -- with no other words or '
    "questions in it."
)
UNIT_REFUSAL = (
    "I could not read that as an apartment or unit, so nothing was saved. Please send "
    'just the unit by itself -- for example "Apt 4B" -- or tell me if there is not one.'
)
CITY_REFUSAL = (
    "I could not read that as a town or city, so nothing was saved. Please send just "
    'the town or city name by itself -- for example "Springfield".'
)
STATE_REFUSAL = (
    "I could not read that as a US state, so nothing was saved. Please send the "
    'two-letter state code -- for example "NJ" for New Jersey.'
)
ZIP_REFUSAL = (
    "I could not read that as a ZIP code, so nothing was saved. Please send the "
    'five-digit ZIP code -- for example "08742".'
)
NAME_REFUSAL = (
    "I could not read that as a name, so nothing was saved. Please send just the name "
    "by itself, with no other words around it."
)
DATE_OF_BIRTH_REFUSAL = (
    "I could not read that as a date of birth, so nothing was saved. Please send it as "
    'a day, month and year -- for example "1 April 1985".'
)
NOTHING_TO_CHANGE_REFUSAL = (
    "I did not catch which detail you wanted to change, so nothing was saved. Please "
    "tell me whether it is your name or your date of birth, and what it should be."
)
APPOINTMENT_REFUSAL = (
    "I could not match that to one of your appointments, so nothing was changed. Please "
    "ask me to list your appointments and then tell me which one you mean."
)
SLOT_REFUSAL = (
    "I could not match that to one of the times I offered you, so nothing was booked. "
    "Please ask me for available times again and then tell me which one you would like."
)
UNVALIDATABLE_WRITE_REFUSAL = (
    "Sorry -- I could not safely make that change, so nothing was saved. Please contact "
    "the clinic directly and they can help."
)

# One per assessment field: naming the choices back in plain words is what makes a
# refusal actionable, and it exposes nothing the patient was not already asked.
_ANSWER_REFUSALS: Mapping[str, str] = MappingProxyType(
    {
        "preferred_contact": (
            "I could not record how you would like to be contacted, so nothing was "
            "saved. Please tell me whether you prefer a phone call, an email, or a "
            "message in this portal -- and give me the number or address if it is one "
            "of the first two."
        ),
        "help_type": (
            "I could not record what kind of help you are looking for, so nothing was "
            "saved. Please tell me whether it is counselling or therapy, psychiatric "
            "evaluation or medication support, both, or that you are not sure yet."
        ),
        "visit_preference": (
            "I could not record how you would like to be seen, so nothing was saved. "
            "Please tell me both whether you prefer in person or video, and roughly "
            "when suits you -- a weekday morning, afternoon, evening, or a weekend."
        ),
        "accommodations": (
            "I could not record the support you need at your visit, so nothing was "
            "saved. Please tell me if you need an interpreter, or hearing, vision or "
            "mobility support -- or that you do not need any."
        ),
    }
)

_NO_ACCOMMODATIONS_ANSWERS = frozenset({"none", "no", "no_accommodations", "nothing"})


class ValidationRefused(ToolSurfaceError):
    """Raised when a proposed field could not be vouched for; nothing was written.

    A `ToolSurfaceError` so that a caller degrading a turn already handles it, and so
    every refusal on the model path -- malformed call, unknown tool, unvalidatable
    field -- reaches the patient the same way, through `exc.patient_message`.
    `details` is for the log and the developer and is never shown to the patient.
    """

    def __init__(self, details: list[str], *, patient_message: str) -> None:
        self.details = details
        super().__init__(
            details[0] if details else "field validation refused", patient_message=patient_message
        )


@dataclass(frozen=True)
class WriteContext:
    """What the application knows independently of the model, at this moment.

    `offered_appointments` and `offered_slots` are the anonymous tokens this patient's
    own session was actually issued (`AppointmentDiscoveryService`, `SlotDiscoveryService`),
    which is how "an appointment reference resolves to one this patient actually has"
    is checked without asking the model to be right about it. Membership is checked
    rather than `AnonymousAppointmentStore.resolve()` being called, because resolving
    consumes a single-use token: validation must not spend the token the confirmed
    write still needs.
    """

    now: datetime
    offered_appointments: tuple[AnonymousAppointmentToken, ...] = ()
    offered_slots: tuple[AnonymousSlotToken, ...] = ()


@dataclass(frozen=True)
class ValidatedWrite:
    """Values a writing tool may propose to the patient, and nothing the model said.

    Constructing one is only possible through `validate_write()`, so the existence of a
    `ValidatedWrite` is itself the evidence that every field passed. `values` are the
    validated, normalised values in the shape the backing service takes.
    """

    tool: str
    values: Mapping[str, Any]
    context: WriteContext = field(compare=False)


def validate_write(
    tool: str, arguments: Mapping[str, Any], *, context: WriteContext
) -> ValidatedWrite:
    """Validate every field of a writing tool's proposed arguments, or refuse.

    `arguments` is a plain mapping on purpose: this function is not given the model's
    call object and cannot be tempted to read a value out of it. A tool with no
    validator here is refused rather than executed, so adding a writing tool without
    validating its fields fails closed.
    """
    validator = _WRITE_VALIDATORS.get(tool)
    if validator is None:
        logger.warning("write refused: no validator for tool=%s", _loggable(tool))
        raise ValidationRefused(
            [f"{tool} is not a writing tool with field validation"],
            patient_message=UNVALIDATABLE_WRITE_REFUSAL,
        )
    values = validator(arguments, context)
    return ValidatedWrite(tool=tool, values=MappingProxyType(dict(values)), context=context)


def confirmation_prompt(validated: ValidatedWrite) -> str:
    """Render the confirmation the patient is asked to approve, or refuse to render it.

    Takes only a `ValidatedWrite`, so the model's proposed values are not reachable
    from here, and re-validates before rendering, so a value the validator cannot vouch
    for never appears in a confirmation prompt (AC3). That is the specific failure
    TICK-050 shipped: its review step rendered its parser's output unchecked.
    """
    checked = _revalidate(validated)
    return _CONFIRMATION_RENDERERS[validated.tool](checked.values, checked.context)


def write_authority(validated: ValidatedWrite, *, patient_confirmed: bool) -> WriteAuthority:
    """Mint the evidence `executable_arguments()` requires -- the only place that happens.

    Validates a third and final time and builds the authority from *that* result, so the
    values a backing service is called with are the validators' own output. Confirmation
    is recorded faithfully rather than assumed: an unconfirmed authority is still refused
    by `ai_server.llm.tools.executable_arguments`, which is where that rule lives.
    """
    checked = _revalidate(validated)
    return WriteAuthority(
        tool=checked.tool,
        validated_arguments=dict(checked.values),
        patient_confirmed=patient_confirmed,
    )


def _revalidate(validated: ValidatedWrite) -> ValidatedWrite:
    """Re-run validation over already-validated values and refuse if anything moved."""
    checked = validate_write(validated.tool, validated.values, context=validated.context)
    if dict(checked.values) != dict(validated.values):
        logger.warning("write refused: values did not survive revalidation tool=%s", validated.tool)
        raise ValidationRefused(
            [f"{validated.tool} values did not survive revalidation"],
            patient_message=UNVALIDATABLE_WRITE_REFUSAL,
        )
    return checked


# --- Field validators, each reusable on its own ------------------------------------


def _refuse(exc: FieldValidationError, patient_message: str) -> ValidationRefused:
    return ValidationRefused(list(exc.details), patient_message=patient_message)


def validate_person_name(value: object, *, label: str) -> str:
    """Return `value` as a person's name, or refuse it."""
    try:
        return validate_text_name(value, label=label)
    except FieldValidationError as exc:
        raise _refuse(exc, NAME_REFUSAL) from exc


def validate_birth_date(value: object, *, now: datetime) -> str:
    """Return `value` as an ISO date that is a plausible adult's date of birth."""
    try:
        return validate_date_of_birth(value, now=now)
    except FieldValidationError as exc:
        raise _refuse(exc, DATE_OF_BIRTH_REFUSAL) from exc


def validate_mailing_address(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the validated address components, or refuse the whole address.

    The refusal names the first component that failed, so a patient who mistyped only a
    ZIP is not asked to re-send the street as well.
    """
    try:
        address = validate_address(dict(value))
    except FieldValidationError as exc:
        raise _refuse(exc, _address_refusal(exc.details)) from exc
    validated: dict[str, Any] = {
        "street1": address.street1,
        "city": address.city,
        "state": address.state,
        "zip_code": address.zip_code,
    }
    if address.street2 is not None:
        validated["street2"] = address.street2
    return validated


def _address_refusal(details: list[str]) -> str:
    """Pick the patient sentence for the first address component that failed."""
    first = details[0] if details else ""
    for prefix, message in (
        ("street1", STREET_REFUSAL),
        ("street2", UNIT_REFUSAL),
        ("city", CITY_REFUSAL),
        ("state", STATE_REFUSAL),
        ("zip_code", ZIP_REFUSAL),
    ):
        if first.startswith(prefix):
            return message
    return UNVALIDATABLE_WRITE_REFUSAL


def validate_offered_appointment(
    value: object, *, context: WriteContext
) -> AnonymousAppointmentToken:
    """Return the offered appointment `value` refers to, or refuse it.

    "One this patient actually has" is decided by what this session was issued from the
    patient's own OpenEMR appointments -- never by the token looking well-formed, which
    is all a pattern could tell us and all the model could imitate.
    """
    for offered in context.offered_appointments:
        if value == offered.appointment_token:
            return offered
    logger.warning("write refused: appointment reference matched nothing offered")
    raise ValidationRefused(
        ["appointment_token does not match an appointment offered to this patient"],
        patient_message=APPOINTMENT_REFUSAL,
    )


def validate_offered_slot(value: object, *, context: WriteContext) -> AnonymousSlotToken:
    """Return the offered slot `value` refers to, or refuse it."""
    for offered in context.offered_slots:
        if value == offered.slot_token:
            return offered
    logger.warning("write refused: slot reference matched nothing offered")
    raise ValidationRefused(
        ["slot_token does not match a slot offered to this patient"],
        patient_message=SLOT_REFUSAL,
    )


def validate_assessment_answer(field_name: object, answer: object) -> tuple[str, str]:
    """Return `(field, canonical answer)` for one assessment answer, or refuse it.

    The model proposes free text; the record takes one of a closed set of choices.
    Mapping the two is a judgement, so it is made here by exact match against the
    choices the patient was offered, after case and spacing are normalised -- never by
    guessing at the nearest one. Anything that does not land exactly on a choice is
    refused, with that field's choices named back to the patient in plain words.
    """
    if not isinstance(field_name, str) or field_name not in DRAFT_FIELDS:
        logger.warning("write refused: unknown assessment field")
        raise ValidationRefused(
            ["field is not one of the assessment fields"],
            patient_message=UNVALIDATABLE_WRITE_REFUSAL,
        )
    refusal = _ANSWER_REFUSALS[field_name]
    if not isinstance(answer, str) or not answer.strip():
        raise ValidationRefused([f"{field_name} answer must be text"], patient_message=refusal)
    text = " ".join(answer.split())
    try:
        return field_name, _ANSWER_CANONICALISERS[field_name](text)
    except FieldValidationError as exc:
        raise _refuse(exc, refusal) from exc


def _code(text: str) -> str:
    return text.strip().lower().replace("-", "_").replace(" ", "_")


def _canonical_preferred_contact(text: str) -> str:
    """`portal_message`, or a method and the contact value it needs, e.g. `email a@b.c`."""
    head, _, rest = text.partition(" ")
    method = _code(head)
    if method not in CONTACT_METHODS:
        raise FieldValidationError([f"method must be one of: {', '.join(CONTACT_METHODS)}"])
    if method == "portal_message":
        validate_preferred_contact({"method": method})
        return method
    validate_preferred_contact({"method": method, "value": rest.strip()})
    return f"{method} {rest.strip()}"


def _canonical_help_type(text: str) -> str:
    code = _code(text)
    validate_help_type(code)
    return code


def _canonical_visit_preference(text: str) -> str:
    """A visit format and a time window, in that order, e.g. `video,weekday_morning`."""
    parts = [part for part in text.split(",") if part.strip()]
    if len(parts) != 2:
        raise FieldValidationError(
            [
                f"visit_preference needs a format ({', '.join(VISIT_FORMATS)}) and a time "
                f"window ({', '.join(VISIT_TIME_WINDOWS)})"
            ]
        )
    visit_format, time_window = _code(parts[0]), _code(parts[1])
    validate_visit_preference({"format": visit_format, "time_window": time_window})
    return f"{visit_format},{time_window}"


def _canonical_accommodations(text: str) -> str:
    """A comma-separated set of accommodation codes, or `none`."""
    if _code(text) in _NO_ACCOMMODATIONS_ANSWERS:
        validate_accommodations({"selected": []})
        return "none"
    selected = [_code(part) for part in text.split(",") if part.strip()]
    validate_accommodations({"selected": selected})
    if not selected:
        raise FieldValidationError([f"selected may only contain: {', '.join(ACCOMMODATIONS)}"])
    return ",".join(selected)


_ANSWER_CANONICALISERS: Mapping[str, Callable[[str], str]] = MappingProxyType(
    {
        "preferred_contact": _canonical_preferred_contact,
        "help_type": _canonical_help_type,
        "visit_preference": _canonical_visit_preference,
        "accommodations": _canonical_accommodations,
    }
)


# --- Per-tool validation -----------------------------------------------------------


def _validate_update_address(arguments: Mapping[str, Any], context: WriteContext) -> dict[str, Any]:
    return validate_mailing_address(arguments)


def _validate_update_demographics(
    arguments: Mapping[str, Any], context: WriteContext
) -> dict[str, Any]:
    """Validate whichever identity fields were proposed; refuse if none were.

    The emptiness check is repeated here rather than left to the tool schema: this
    module does not trust that the value reaching it came through a schema at all.
    """
    validated: dict[str, Any] = {}
    for key, label in (("given_name", "given_name"), ("family_name", "family_name")):
        if arguments.get(key) is not None:
            validated[key] = validate_person_name(arguments.get(key), label=label)
    if arguments.get("date_of_birth") is not None:
        validated["date_of_birth"] = validate_birth_date(
            arguments.get("date_of_birth"), now=context.now
        )
    if not validated:
        raise ValidationRefused(
            ["update_demographics was proposed with no field to change"],
            patient_message=NOTHING_TO_CHANGE_REFUSAL,
        )
    return validated


def _validate_record_assessment_answer(
    arguments: Mapping[str, Any], context: WriteContext
) -> dict[str, Any]:
    field_name, answer = validate_assessment_answer(arguments.get("field"), arguments.get("answer"))
    return {"field": field_name, "answer": answer}


def _validate_book_appointment(
    arguments: Mapping[str, Any], context: WriteContext
) -> dict[str, Any]:
    offered = validate_offered_slot(arguments.get("slot_token"), context=context)
    return {"slot_token": offered.slot_token}


def _validate_cancel_appointment(
    arguments: Mapping[str, Any], context: WriteContext
) -> dict[str, Any]:
    offered = validate_offered_appointment(arguments.get("appointment_token"), context=context)
    return {"appointment_token": offered.appointment_token}


_WRITE_VALIDATORS: Mapping[str, Callable[[Mapping[str, Any], WriteContext], Mapping[str, Any]]] = (
    MappingProxyType(
        {
            "update_address": _validate_update_address,
            "update_demographics": _validate_update_demographics,
            "record_assessment_answer": _validate_record_assessment_answer,
            "book_appointment": _validate_book_appointment,
            "cancel_appointment": _validate_cancel_appointment,
        }
    )
)

VALIDATED_WRITING_TOOLS: frozenset[str] = frozenset(_WRITE_VALIDATORS)


# --- Confirmation rendering --------------------------------------------------------
#
# Every renderer takes only validated values. None of them is given the tool call, so
# none of them can echo a proposal even by accident, and each ends by saying plainly
# that nothing has been saved yet -- the same discipline `address_chat._review_summary`
# established for the flow this replaces.

_CONFIRM_INSTRUCTION = (
    "\n\nReply CONFIRM to save this, or tell me what to change. Nothing is saved until you confirm."
)


def _confirm_update_address(values: Mapping[str, Any], context: WriteContext) -> str:
    lines = [f"Street: {values['street1']}"]
    if values.get("street2"):
        lines.append(f"Apartment or unit: {values['street2']}")
    lines += [
        f"City: {values['city']}",
        f"State: {values['state']}",
        f"ZIP code: {values['zip_code']}",
    ]
    body = "\n".join(lines)
    return (
        "Here is the mailing address I am about to save. Nothing has been saved yet -- "
        f"please check it:\n{body}{_CONFIRM_INSTRUCTION}"
    )


_DEMOGRAPHIC_LABELS = (
    ("given_name", "Given name"),
    ("family_name", "Family name"),
    ("date_of_birth", "Date of birth"),
)


def _confirm_update_demographics(values: Mapping[str, Any], context: WriteContext) -> str:
    body = "\n".join(
        f"{label}: {values[key]}" for key, label in _DEMOGRAPHIC_LABELS if key in values
    )
    return (
        "Here is the detail I am about to change. Nothing has been saved yet -- please "
        f"check it:\n{body}{_CONFIRM_INSTRUCTION}"
    )


_ANSWER_QUESTIONS: Mapping[str, str] = MappingProxyType(
    {
        "preferred_contact": "How you would like to be contacted",
        "help_type": "What kind of help you are looking for",
        "visit_preference": "How and when you would like to be seen",
        "accommodations": "Support you need at your visit",
    }
)


def _confirm_record_assessment_answer(values: Mapping[str, Any], context: WriteContext) -> str:
    question = _ANSWER_QUESTIONS[values["field"]]
    answer = _readable_answer(values["field"], values["answer"])
    return (
        "Here is the answer I am about to record. Nothing has been saved yet -- please "
        f"check it:\n{question}: {answer}{_CONFIRM_INSTRUCTION}"
    )


def _readable_answer(field_name: str, answer: str) -> str:
    """Turn a canonical answer back into the plain words the patient used."""
    if field_name == "preferred_contact":
        method, _, value = answer.partition(" ")
        readable = method.replace("_", " ")
        return f"{readable}, {value}" if value else readable
    return ", ".join(part.replace("_", " ") for part in answer.split(","))


def _confirm_book_appointment(values: Mapping[str, Any], context: WriteContext) -> str:
    """Show the time the *store* recorded for the offered slot, not one the model named.

    Re-resolving through `validate_offered_slot` is what makes this a check: the times
    shown come from what this patient's session was offered, so a confirmation cannot
    read back a time that no slot has.
    """
    offered = validate_offered_slot(values["slot_token"], context=context)
    return (
        "Here is the appointment I am about to book. Nothing has been booked yet -- "
        f"please check it:\n{_window(offered.starts_at, offered.ends_at)}"
        f"{_CONFIRM_INSTRUCTION}"
    )


def _confirm_cancel_appointment(values: Mapping[str, Any], context: WriteContext) -> str:
    """Show the time of the patient's own appointment, re-resolved rather than echoed."""
    offered = validate_offered_appointment(values["appointment_token"], context=context)
    return (
        "Here is the appointment I am about to cancel. Nothing has been cancelled yet "
        f"-- please check it:\n{_window(offered.starts_at, offered.ends_at)}"
        f"{_CONFIRM_INSTRUCTION}"
    )


def _window(starts_at: datetime, ends_at: datetime) -> str:
    return (
        f"{starts_at:%A} {starts_at.day} {starts_at:%B %Y}, "
        f"{_clock(starts_at)} to {_clock(ends_at)}"
    )


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


_CONFIRMATION_RENDERERS: Mapping[str, Callable[[Mapping[str, Any], WriteContext], str]] = (
    MappingProxyType(
        {
            "update_address": _confirm_update_address,
            "update_demographics": _confirm_update_demographics,
            "record_assessment_answer": _confirm_record_assessment_answer,
            "book_appointment": _confirm_book_appointment,
            "cancel_appointment": _confirm_cancel_appointment,
        }
    )
)


def _loggable(tool: object) -> str:
    """Keep an invented tool name out of the log unless it is plainly a name."""
    if isinstance(tool, str) and tool in WRITING_TOOLS:
        return tool
    return "<unprintable>"
