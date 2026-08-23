"""The complete set of actions the model may ask for, and the contract it must ask in.

TICK-060, from `docs/LOCAL_LLM_SPEC.md` D6 ("model proposes, code disposes") and D9
("the model owns all turn routing"). The model never acts. Per turn it emits exactly
one tool call under a strict schema; this module decides whether that call is even
admissible, and application code executes it afterwards.

Every tool re-faces a service that already exists, so this surface adds no capability:

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
| `ask_general_knowledge` | Groq, via the D14 restatement | no |
| `reply` | none | no |

**Why `RescheduleService` is absent.** TICK-020 established that OpenEMR exposes no
service method for rescheduling; `ai_server/scheduling/reschedule.py` is a
cancel-then-book sequence, not an atomic move, and its own
`RescheduleCancellationFailedError` exists because that sequence can strand a patient
with neither appointment. A tool is a promise the model is free to make on its own
judgement (D9), so publishing one the system cannot honour invites exactly that
promise. Rescheduling therefore reaches the patient as what it actually is: a
`cancel_appointment` and a `book_appointment`, each confirmed separately. The existing
reschedule path is untouched by this ticket -- it is simply not on this surface.

**Two halves of one guarantee (AC1).** Where the runtime supports constrained decoding,
`tool_call_json_schema()` is the grammar, so a malformed call cannot be generated at
all. Where it does not -- and no runtime is trusted to have honoured it -- every
response still passes `parse_tool_call()`, which either returns a fully validated call
or raises. There is no partial path: the argument models are constructed all-at-once by
Pydantic, so a call that fails validation has never had a single field handed onward.

**Shape, not content.** The argument models here bound structure: which fields exist,
their types, their lengths, and that references are anonymous tokens. They deliberately
do *not* judge whether a state code is real or a date of birth is plausible -- that is
TICK-061's field-level validation, which every writing tool must pass through
regardless (see `WriteAuthority`). A schema that looked authoritative about content
would invite callers to skip that step.

**No OpenEMR identifiers (AC5).** No argument on this surface names an OpenEMR record.
Appointments and slots are referenced only by the existing single-use anonymous tokens
(`ai_server/scheduling/appointments.py`, `ai_server/scheduling/slots.py`), and document
uploads only by the local handle `OcrService.begin()` minted. Those stores are the only
resolvers, so a token the model invents resolves to nothing and reaches no service. The
caller's own identity is never an argument either: it comes from the session's bearer
token, server-side, on every service call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

logger = logging.getLogger(__name__)

# The name given to the schema when it is handed to a runtime's structured-output mode.
TOOL_CALL_SCHEMA_NAME = "tool_call"

# Mirrors the wire patterns `AnonymousSlot`/`AnonymousAppointment` already enforce in
# `ai_server/privacy/gate.py`; `test_tool_surface.py` asserts the three stay identical
# rather than leaving a second copy free to drift.
SLOT_TOKEN_PATTERN = "^slot_[A-Za-z0-9_-]{1,64}$"
APPOINTMENT_TOKEN_PATTERN = "^appt_[A-Za-z0-9_-]{1,64}$"
# `OcrUploadStore.begin()` mints `secrets.token_urlsafe(18)` -- unprefixed, unlike the
# scheduling tokens, so this bounds the alphabet and length instead.
UPLOAD_ID_PATTERN = "^[A-Za-z0-9_-]{16,64}$"

# Patient-visible behaviour for each defined refusal (AC2/AC4). A refused turn is a
# turn the chat answers, not an exception that reaches the transcript. These follow
# `PLANNING_FAILED_RESPONSE` in `ai_server/llm/groq.py`: say nothing was done, do not
# name the internal fault, and offer the patient a next step.
TOOL_CALL_REFUSED_RESPONSE = (
    "Sorry -- I could not work out how to handle that request, so nothing was done. "
    "Please try rewording it, or contact the clinic directly if it is something this "
    "chat cannot do."
)
UNKNOWN_TOOL_RESPONSE = (
    "Sorry -- that is not something this chat can do, so nothing was done. Please "
    "contact the clinic directly and they can help."
)
UNCONFIRMED_WRITE_RESPONSE = (
    "Sorry -- I did not have your confirmation for that change, so nothing was saved. "
    "Please try again and confirm the details when I read them back to you."
)

_MAX_NAME_LENGTH = 100
_MAX_ANSWER_LENGTH = 200
_MAX_RESTATEMENT_LENGTH = 500
_MAX_REPLY_LENGTH = 2_000
_PRINTABLE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class ToolSurfaceError(Exception):
    """Base for every defined refusal, carrying the sentence the patient sees.

    Callers degrade the turn with `exc.patient_message` rather than deciding for
    themselves what a refusal sounds like, so no refusal path can leave the patient
    with a stack trace, a silence, or an invented apology.
    """

    def __init__(self, message: str, *, patient_message: str) -> None:
        super().__init__(message)
        self.patient_message = patient_message


class NoToolCallError(ToolSurfaceError):
    """Raised when a turn produced no tool call at all."""

    def __init__(self) -> None:
        super().__init__(
            "the model produced no tool call", patient_message=TOOL_CALL_REFUSED_RESPONSE
        )


class MultipleToolCallsError(ToolSurfaceError):
    """Raised when a turn produced more than one tool call.

    Refused rather than resolved by taking the first: the turn contract is exactly one
    call (D6), and picking one of several would execute an action the model expressed
    only half a commitment to.
    """

    def __init__(self, count: int | None = None) -> None:
        detail = "more than one" if count is None else str(count)
        super().__init__(
            f"the model produced {detail} tool calls; exactly one is allowed",
            patient_message=TOOL_CALL_REFUSED_RESPONSE,
        )


class UnknownToolError(ToolSurfaceError):
    """Raised when the model named a tool this surface does not publish.

    Reported, never silently dropped (AC4): an invented capability that no-ops looks
    to the patient exactly like a capability that worked.
    """

    def __init__(self, tool_name: object) -> None:
        self.tool_name = tool_name
        super().__init__(
            f"unknown tool name: {_loggable_tool_name(tool_name)}",
            patient_message=UNKNOWN_TOOL_RESPONSE,
        )


class MalformedToolCallError(ToolSurfaceError):
    """Raised when a tool call does not satisfy its schema; nothing was executed."""

    def __init__(self, details: list[str]) -> None:
        self.details = details
        super().__init__(
            details[0] if details else "malformed tool call",
            patient_message=TOOL_CALL_REFUSED_RESPONSE,
        )


class WriteAuthorityError(ToolSurfaceError):
    """Raised when a writing tool was reached without validation and confirmation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, patient_message=UNCONFIRMED_WRITE_RESPONSE)


class _Arguments(BaseModel):
    """Every argument object is closed: an unrecognised key is a malformed call."""

    model_config = ConfigDict(extra="forbid")


class UpdateAddressArguments(_Arguments):
    """A mailing address as the patient stated it, before TICK-061 judges its content."""

    street1: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    street2: str | None = Field(default=None, max_length=_MAX_NAME_LENGTH)
    city: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    state: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    zip_code: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)


class UpdateDemographicsArguments(_Arguments):
    """The identity fields, each optional so one can be corrected without the others."""

    given_name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)
    family_name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)
    date_of_birth: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)

    @model_validator(mode="after")
    def require_one_field(self) -> UpdateDemographicsArguments:
        """Refuse an empty update: a write tool called with nothing to write is a
        confirmation step the patient would be asked to sit through for no change."""
        if self.given_name is None and self.family_name is None and self.date_of_birth is None:
            raise ValueError("update_demographics requires at least one field")
        return self


class RecordAssessmentAnswerArguments(_Arguments):
    """One assessment answer. `field` is closed to the four draft fields
    `ai_server.onboarding.fields.validate_field` accepts, so a constrained runtime
    cannot generate a field name that has no validator; the identity fields are reached
    through `update_demographics`/`update_address` instead."""

    field: Literal["preferred_contact", "help_type", "visit_preference", "accommodations"]
    answer: str = Field(min_length=1, max_length=_MAX_ANSWER_LENGTH)


class ListAppointmentsArguments(_Arguments):
    """No arguments. Whose appointments to list follows from the session's bearer
    token server-side; a patient reference here would be one the model could invent."""


class FindSlotsArguments(_Arguments):
    """No arguments. `SlotDiscoveryService.open_slots()` takes no filter, and a
    preference argument it silently dropped would be the same broken promise that keeps
    `RescheduleService` off this surface."""


class BookAppointmentArguments(_Arguments):
    """Books a slot the patient was actually offered this turn.

    `slot_token` is single-use and issued by `AnonymousSlotStore`; an invented one
    fails `resolve()` before `BookingService` makes any OpenEMR request."""

    slot_token: str = Field(pattern=SLOT_TOKEN_PATTERN)


class CancelAppointmentArguments(_Arguments):
    """Cancels one of the caller's own appointments.

    `appointment_token` is single-use, issued by `AnonymousAppointmentStore`, and bound
    to the patient it was issued for, so it cannot address another patient's record."""

    appointment_token: str = Field(pattern=APPOINTMENT_TOKEN_PATTERN)


class ExtractDocumentFieldsArguments(_Arguments):
    """Reads a document the patient already uploaded under consent.

    `upload_id` is the handle `OcrService.begin()` returned; the upload store is its
    only resolver and expires it, so it references no OpenEMR record."""

    upload_id: str = Field(pattern=UPLOAD_ID_PATTERN)


class AskGeneralKnowledgeArguments(_Arguments):
    """The D14 restatement: a canonical, context-free question this system composed.

    The patient's own words are never the argument -- this is the only field on the
    surface destined to leave the deployment, and it still passes the Presidio gate
    (D4) before it does."""

    restatement: str = Field(min_length=1, max_length=_MAX_RESTATEMENT_LENGTH)


class ReplyArguments(_Arguments):
    """A plain conversational answer, backed by no service and writing nothing."""

    message: str = Field(min_length=1, max_length=_MAX_REPLY_LENGTH)


class _Call(BaseModel):
    """Every call is closed too: `tool` and `arguments`, nothing else."""

    model_config = ConfigDict(extra="forbid")


class UpdateAddressCall(_Call):
    """Update the patient's mailing address."""

    tool: Literal["update_address"]
    arguments: UpdateAddressArguments


class UpdateDemographicsCall(_Call):
    """Correct the patient's name or date of birth."""

    tool: Literal["update_demographics"]
    arguments: UpdateDemographicsArguments


class RecordAssessmentAnswerCall(_Call):
    """Record one answer against the patient's assessment draft."""

    tool: Literal["record_assessment_answer"]
    arguments: RecordAssessmentAnswerArguments


class ListAppointmentsCall(_Call):
    """List the patient's own upcoming appointments."""

    tool: Literal["list_appointments"]
    arguments: ListAppointmentsArguments


class FindSlotsCall(_Call):
    """Offer the currently open appointment slots."""

    tool: Literal["find_slots"]
    arguments: FindSlotsArguments


class BookAppointmentCall(_Call):
    """Book one slot the patient was offered."""

    tool: Literal["book_appointment"]
    arguments: BookAppointmentArguments


class CancelAppointmentCall(_Call):
    """Cancel one of the patient's own appointments."""

    tool: Literal["cancel_appointment"]
    arguments: CancelAppointmentArguments


class ExtractDocumentFieldsCall(_Call):
    """Read fields from a document the patient uploaded."""

    tool: Literal["extract_document_fields"]
    arguments: ExtractDocumentFieldsArguments


class AskGeneralKnowledgeCall(_Call):
    """Ask the external model a question carrying no patient context."""

    tool: Literal["ask_general_knowledge"]
    arguments: AskGeneralKnowledgeArguments


class ReplyCall(_Call):
    """Answer the patient without touching any service."""

    tool: Literal["reply"]
    arguments: ReplyArguments


ToolCall = Annotated[
    Union[
        UpdateAddressCall,
        UpdateDemographicsCall,
        RecordAssessmentAnswerCall,
        ListAppointmentsCall,
        FindSlotsCall,
        BookAppointmentCall,
        CancelAppointmentCall,
        ExtractDocumentFieldsCall,
        AskGeneralKnowledgeCall,
        ReplyCall,
    ],
    Field(discriminator="tool"),
]

_TOOL_CALL_ADAPTER: TypeAdapter[Any] = TypeAdapter(ToolCall)


@dataclass(frozen=True)
class ToolDefinition:
    """One published tool: what runs it, whether it writes, and its argument schema."""

    name: str
    writes: bool
    backed_by: str
    arguments: type[BaseModel]
    call: type[BaseModel]


def _definition(call: type[BaseModel], *, writes: bool, backed_by: str) -> ToolDefinition:
    name = call.model_fields["tool"].annotation.__args__[0]
    arguments = call.model_fields["arguments"].annotation
    return ToolDefinition(
        name=name, writes=writes, backed_by=backed_by, arguments=arguments, call=call
    )


TOOL_SURFACE: Mapping[str, ToolDefinition] = MappingProxyType(
    {
        definition.name: definition
        for definition in (
            _definition(
                UpdateAddressCall, writes=True, backed_by="PatientDemographicsUpdateService"
            ),
            _definition(
                UpdateDemographicsCall, writes=True, backed_by="PatientDemographicsUpdateService"
            ),
            _definition(
                RecordAssessmentAnswerCall, writes=True, backed_by="AssessmentDraftService"
            ),
            _definition(
                ListAppointmentsCall, writes=False, backed_by="AppointmentDiscoveryService"
            ),
            _definition(FindSlotsCall, writes=False, backed_by="SlotDiscoveryService"),
            _definition(BookAppointmentCall, writes=True, backed_by="BookingService"),
            _definition(CancelAppointmentCall, writes=True, backed_by="CancellationService"),
            _definition(ExtractDocumentFieldsCall, writes=False, backed_by="OcrService"),
            _definition(AskGeneralKnowledgeCall, writes=False, backed_by="Groq"),
            _definition(ReplyCall, writes=False, backed_by="none"),
        )
    }
)

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_SURFACE)
WRITING_TOOLS: frozenset[str] = frozenset(
    name for name, definition in TOOL_SURFACE.items() if definition.writes
)


def tool_call_json_schema() -> dict[str, Any]:
    """Return the JSON Schema a runtime constrains generation with.

    Handed to a structured-output mode this is the whole of AC1's first half: the
    grammar admits one object, one of ten tool names, and that name's own closed
    argument object -- so a second call, an invented name, or an unknown argument
    cannot be sampled. It is offered as the plain Pydantic schema; adapting it to a
    vendor's dialect belongs behind that vendor's client, the way `_strict_schema()`
    keeps Groq's `required` quirk behind `HttpGroqClient` (LOCAL_LLM_SPEC Risks).
    """
    return _TOOL_CALL_ADAPTER.json_schema()


def tool_definition(name: str) -> ToolDefinition:
    """Return the published definition for `name`, or raise `UnknownToolError`."""
    definition = TOOL_SURFACE.get(name)
    if definition is None:
        raise UnknownToolError(name)
    return definition


def parse_tool_call(raw: str) -> Any:
    """Return the one validated tool call in `raw`, or raise a defined refusal.

    This is AC1's second half, for any runtime that did not constrain generation.
    Nothing is executed and no argument is read out until the whole call has validated,
    so a refused call is never partially applied.
    """
    payload = _one_json_value(raw)
    if not isinstance(payload, dict):
        raise MalformedToolCallError(["a tool call must be a JSON object"])
    name = payload.get("tool")
    # Checked before schema validation so an invented name is reported as the invented
    # capability it is, rather than as an indistinguishable discriminator mismatch.
    if not isinstance(name, str) or name not in TOOL_SURFACE:
        logger.warning("tool call refused: unknown tool name=%s", _loggable_tool_name(name))
        raise UnknownToolError(name)
    try:
        return _TOOL_CALL_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        details = [f"{_location(error)}: {error['msg']}" for error in exc.errors()]
        logger.warning("tool call refused: malformed call tool=%s", name)
        raise MalformedToolCallError(details) from exc


@dataclass(frozen=True)
class WriteAuthority:
    """Evidence that a writing tool passed validation and the patient confirmed it.

    Produced by TICK-061's validation together with the confirmation step, never by
    this module: the surface can say a write is *permitted*, only those two can say a
    write is *authorised*. `validated_arguments` are the validated values, which is why
    `executable_arguments()` returns them in place of the model's.
    """

    tool: str
    validated_arguments: Mapping[str, Any]
    patient_confirmed: bool


def executable_arguments(
    call: Any, *, authority: WriteAuthority | None = None
) -> Mapping[str, Any]:
    """Return the arguments a backing service may actually be called with.

    A read-only tool executes on its validated arguments directly. A writing tool never
    does: it executes on `authority.validated_arguments`, which only TICK-061's
    field-level validation and the patient confirmation step can produce. Passing this
    surface's schema is therefore necessary for a write and never sufficient for one
    (AC3) -- the model's own argument values are not what reaches the record.
    """
    definition = tool_definition(call.tool)
    if not definition.writes:
        return call.arguments.model_dump()
    if authority is None:
        raise WriteAuthorityError(f"{call.tool} writes and was reached without authority")
    if authority.tool != call.tool:
        raise WriteAuthorityError(f"authority for {authority.tool} cannot execute {call.tool}")
    if not authority.patient_confirmed:
        raise WriteAuthorityError(f"{call.tool} was not confirmed by the patient")
    return dict(authority.validated_arguments)


def _one_json_value(raw: str) -> Any:
    """Decode exactly one JSON value from `raw`, refusing none and refusing several."""
    text = raw.strip()
    if not text:
        raise NoToolCallError()
    try:
        value, consumed = json.JSONDecoder().raw_decode(text)
    except ValueError as exc:
        raise MalformedToolCallError(["response is not valid JSON"]) from exc
    remainder = text[consumed:].strip()
    if remainder:
        # A second JSON value is a second call; anything else is simply not a call.
        if remainder[0] in "{[":
            raise MultipleToolCallsError()
        raise MalformedToolCallError(["trailing content after the tool call"])
    if isinstance(value, list):
        # A runtime that wraps its single call in an array has still emitted one call;
        # the rule being enforced is the count, not the packaging.
        if not value:
            raise NoToolCallError()
        if len(value) > 1:
            raise MultipleToolCallsError(len(value))
        return value[0]
    return value


def _location(error: Mapping[str, Any]) -> str:
    location = error.get("loc") or ()
    return ".".join(str(part) for part in location) or "tool call"


def _loggable_tool_name(name: object) -> str:
    """Keep a model-invented name out of the log unless it is plainly a name.

    The string is model output, so it is not assumed to be a tool name at all; logging
    it unconditionally would be a route for patient text to reach the log.
    """
    if isinstance(name, str) and _PRINTABLE_TOOL_NAME.match(name):
        return name
    return "<unprintable>"
