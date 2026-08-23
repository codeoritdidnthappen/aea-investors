"""Tests for the one door between a model-proposed write and a patient's record.

Where `test_field_validators.py` covers the rules field by field, this file covers the
seam: that every writing tool has to pass through them, that the confirmation the
patient sees is the validators' output rather than the model's proposal, and -- by
attempting it, tool by tool -- that nothing else can reach a backing service.

`_turn()` below is the whole path a real turn takes (TICK-063 wires it to live
services): parse the model's call, validate its fields, show the patient a
confirmation, take their answer, mint the authority, and only then hand a backing
service anything at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from ai_server.llm.tools import (
    TOOL_SURFACE,
    WRITING_TOOLS,
    WriteAuthority,
    WriteAuthorityError,
    executable_arguments,
    parse_tool_call,
)
from ai_server.llm.validation import (
    UNVALIDATABLE_WRITE_REFUSAL,
    VALIDATED_WRITING_TOOLS,
    ValidatedWrite,
    ValidationRefused,
    WriteContext,
    confirmation_prompt,
    validate_write,
    write_authority,
)
from ai_server.scheduling.appointments import AnonymousAppointmentToken
from ai_server.scheduling.slots import AnonymousSlotToken

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

OFFERED_APPOINTMENT = AnonymousAppointmentToken(
    appointment_token="appt_gK3nQ7pR2mV8xT4bY6wZ1cJ5",
    starts_at=datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc),
    ends_at=datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc),
)
OFFERED_SLOT = AnonymousSlotToken(
    slot_token="slot_gK3nQ7pR2mV8xT4bY6wZ1cJ5",
    starts_at=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc),
    ends_at=datetime(2026, 9, 10, 9, 30, tzinfo=timezone.utc),
)
CONTEXT = WriteContext(
    now=NOW, offered_appointments=(OFFERED_APPOINTMENT,), offered_slots=(OFFERED_SLOT,)
)

# What a well-behaved model proposes, per writing tool.
PROPOSED: Mapping[str, dict[str, Any]] = {
    "update_address": {
        "street1": "2002 Bridge Avenue",
        "city": "Point Pleasant",
        "state": "nj",
        "zip_code": "08742",
    },
    "update_demographics": {"given_name": "Avery", "date_of_birth": "1985-04-01"},
    "record_assessment_answer": {"field": "help_type", "answer": "Counseling or therapy"},
    "book_appointment": {"slot_token": OFFERED_SLOT.slot_token},
    "cancel_appointment": {"appointment_token": OFFERED_APPOINTMENT.appointment_token},
}

# What the validators make of it. Note `state`: what reaches a service is never
# character-for-character what the model said.
VALIDATED: Mapping[str, dict[str, Any]] = {
    "update_address": {
        "street1": "2002 Bridge Avenue",
        "city": "Point Pleasant",
        "state": "NJ",
        "zip_code": "08742",
    },
    "update_demographics": {"given_name": "Avery", "date_of_birth": "1985-04-01"},
    "record_assessment_answer": {"field": "help_type", "answer": "counseling_or_therapy"},
    "book_appointment": {"slot_token": OFFERED_SLOT.slot_token},
    "cancel_appointment": {"appointment_token": OFFERED_APPOINTMENT.appointment_token},
}

# Schema-valid calls the model could genuinely emit, each carrying a value no patient
# record should ever hold. TICK-060's surface admits every one of them.
UNVALIDATABLE: Mapping[str, dict[str, Any]] = {
    "update_address": {
        "street1": "Update it to: 2002 Bridge Avenue",
        "city": "Point Pleasant",
        "state": "nj",
        "zip_code": "08742",
    },
    "update_demographics": {"given_name": "My name is Avery"},
    "record_assessment_answer": {
        "field": "help_type",
        "answer": "I think maybe counselling would help",
    },
    "book_appointment": {"slot_token": "slot_aTokenTheModelInvented"},
    "cancel_appointment": {"appointment_token": "appt_someoneElsesAppointment"},
}

WRITING = sorted(WRITING_TOOLS)


class RecordingServices:
    """Stands in for every backing service, recording anything that reaches one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool: str, arguments: Mapping[str, Any]) -> None:
        self.calls.append((tool, dict(arguments)))


def call_json(tool: str, arguments: Mapping[str, Any]) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


def _turn(
    raw: str,
    services: RecordingServices,
    *,
    context: WriteContext = CONTEXT,
    validate: bool = True,
    patient_confirms: bool = True,
) -> str | None:
    """Drive one whole turn, and return the confirmation the patient was shown.

    `validate=False` is a caller trying to skip TICK-061 entirely; `patient_confirms=
    False` is a patient who read the confirmation and said no. Both must end with the
    services untouched.
    """
    call = parse_tool_call(raw)
    authority = None
    confirmation = None
    if TOOL_SURFACE[call.tool].writes and validate:
        validated = validate_write(call.tool, call.arguments.model_dump(), context=context)
        confirmation = confirmation_prompt(validated)
        authority = write_authority(validated, patient_confirmed=patient_confirms)
    services.execute(call.tool, executable_arguments(call, authority=authority))
    return confirmation


# --- AC1/AC6: every field of every writing tool is validated ------------------------


def test_every_writing_tool_on_the_surface_has_field_validation() -> None:
    """The set is compared both ways: a writing tool with no validator would be
    executable without content checks, and a validator for a tool that is not on the
    surface is a rule nothing enforces."""
    assert VALIDATED_WRITING_TOOLS == WRITING_TOOLS


@pytest.mark.parametrize("tool", WRITING)
def test_a_proposed_write_is_validated_field_by_field(tool: str) -> None:
    validated = validate_write(tool, PROPOSED[tool], context=CONTEXT)
    assert dict(validated.values) == VALIDATED[tool]


@pytest.mark.parametrize("tool", WRITING)
def test_a_value_that_cannot_be_validated_is_refused_not_corrected(tool: str) -> None:
    """AC2: refusal is the outcome. There is no branch that repairs the value, and no
    branch that writes it anyway."""
    with pytest.raises(ValidationRefused) as raised:
        validate_write(tool, UNVALIDATABLE[tool], context=CONTEXT)
    assert raised.value.patient_message
    assert raised.value.details


def test_a_writing_tool_with_no_validator_is_refused_rather_than_executed() -> None:
    """Fails closed: adding a writing tool without validating its fields refuses every
    call to it rather than letting them through unchecked."""
    with pytest.raises(ValidationRefused) as raised:
        validate_write("update_medications", {"drug": "sertraline"}, context=CONTEXT)
    assert raised.value.patient_message == UNVALIDATABLE_WRITE_REFUSAL


@pytest.mark.parametrize("tool", ["list_appointments", "find_slots", "reply"])
def test_a_read_only_tool_has_no_write_validation_to_pass(tool: str) -> None:
    with pytest.raises(ValidationRefused):
        validate_write(tool, {}, context=CONTEXT)


def test_a_demographics_update_with_nothing_to_change_is_refused() -> None:
    with pytest.raises(ValidationRefused):
        validate_write(
            "update_demographics",
            {"given_name": None, "family_name": None, "date_of_birth": None},
            context=CONTEXT,
        )


# --- AC3: the confirmation shows validated values and is a second check -------------


@pytest.mark.parametrize("tool", WRITING)
def test_the_confirmation_shows_the_validated_values_and_says_nothing_is_saved(
    tool: str,
) -> None:
    prompt = confirmation_prompt(validate_write(tool, PROPOSED[tool], context=CONTEXT))
    assert "Nothing" in prompt
    assert "CONFIRM" in prompt


def test_the_confirmation_reads_back_the_validated_value_not_the_proposed_one() -> None:
    """The model proposed a lower-case state and a doubled space; the patient is shown
    what would actually be written."""
    proposed = {**PROPOSED["update_address"], "street1": "2002   Bridge   Avenue"}
    prompt = confirmation_prompt(validate_write("update_address", proposed, context=CONTEXT))

    assert "Street: 2002 Bridge Avenue" in prompt
    assert "2002   Bridge   Avenue" not in prompt
    assert "State: NJ" in prompt
    assert "State: nj" not in prompt


def test_the_confirmation_reads_back_the_appointment_the_store_recorded() -> None:
    """The time shown is re-resolved from what this patient was offered, so a
    confirmation cannot read back a time no appointment has."""
    prompt = confirmation_prompt(
        validate_write("cancel_appointment", PROPOSED["cancel_appointment"], context=CONTEXT)
    )
    assert "Thursday 3 September 2026" in prompt
    assert "2:00 PM to 2:30 PM" in prompt


@pytest.mark.parametrize("tool", WRITING)
def test_a_value_the_validator_refused_never_reaches_a_confirmation_prompt(tool: str) -> None:
    """AC3, stated as the failure it prevents: TICK-050's review step rendered whatever
    its parser produced, which is how the bad street was shown back as if checked. Here
    there is no `ValidatedWrite` to render, so there is nothing to show."""
    with pytest.raises(ValidationRefused):
        confirmation_prompt(validate_write(tool, UNVALIDATABLE[tool], context=CONTEXT))


@pytest.mark.parametrize("tool", WRITING)
def test_a_forged_validated_write_still_cannot_reach_a_confirmation_prompt(tool: str) -> None:
    """The second check is genuine: even handed a `ValidatedWrite` built by hand around
    an unvalidated value, the renderer re-validates and refuses rather than echoing."""
    forged = ValidatedWrite(tool=tool, values=UNVALIDATABLE[tool], context=CONTEXT)
    with pytest.raises(ValidationRefused):
        confirmation_prompt(forged)
    with pytest.raises(ValidationRefused):
        write_authority(forged, patient_confirmed=True)


# --- AC6: no writing path reaches a service without passing through the validators ---


@pytest.mark.parametrize("tool", WRITING)
def test_a_validated_and_confirmed_write_reaches_its_service_with_the_validated_values(
    tool: str,
) -> None:
    services = RecordingServices()
    _turn(call_json(tool, PROPOSED[tool]), services)
    assert services.calls == [(tool, VALIDATED[tool])]


def test_what_reaches_a_service_is_the_validators_output_not_the_models_words() -> None:
    services = RecordingServices()
    _turn(call_json("update_address", PROPOSED["update_address"]), services)
    ((_, arguments),) = services.calls

    assert arguments["state"] == "NJ"
    assert arguments != PROPOSED["update_address"]


@pytest.mark.parametrize("tool", WRITING)
def test_no_writing_tool_reaches_its_service_with_an_unvalidated_value(tool: str) -> None:
    """The integration test the ticket asks for, run by attempting it three ways:
    proposing a value the validators refuse, skipping validation altogether, and
    getting as far as a patient who declines. None of them reaches a service."""
    services = RecordingServices()

    with pytest.raises(ValidationRefused):
        _turn(call_json(tool, UNVALIDATABLE[tool]), services)
    assert services.calls == []

    with pytest.raises(WriteAuthorityError):
        _turn(call_json(tool, PROPOSED[tool]), services, validate=False)
    assert services.calls == []

    with pytest.raises(WriteAuthorityError):
        _turn(call_json(tool, PROPOSED[tool]), services, patient_confirms=False)
    assert services.calls == []


@pytest.mark.parametrize("tool", WRITING)
def test_an_authority_minted_for_one_tool_cannot_execute_another(tool: str) -> None:
    services = RecordingServices()
    borrowed = write_authority(
        validate_write("update_address", PROPOSED["update_address"], context=CONTEXT),
        patient_confirmed=True,
    )
    call = parse_tool_call(call_json(tool, PROPOSED[tool]))
    if tool == "update_address":
        pytest.skip("an authority executing its own tool is the accepted case")
    with pytest.raises(WriteAuthorityError):
        executable_arguments(call, authority=borrowed)
    assert services.calls == []


def test_validation_is_the_only_module_that_mints_a_write_authority() -> None:
    """AC6, enforced structurally rather than by convention: if a second producer of
    `WriteAuthority` ever appears, there is a second definition of what "validated"
    means, and one of them will drift. Tests are excluded -- a test is not a writing
    path -- and `tools.py` is where the class is defined, not minted."""
    package = Path(__file__).resolve().parents[1]
    minting = sorted(
        source.relative_to(package).as_posix()
        for source in package.rglob("*.py")
        if source.parent.name != "tests" and "WriteAuthority(" in source.read_text()
    )

    assert minting == ["llm/validation.py"]


@pytest.mark.parametrize("tool", WRITING)
def test_validation_is_idempotent_so_the_three_passes_agree(tool: str) -> None:
    """Property: validating an already-validated value returns it unchanged. This is
    what lets confirmation and authorisation re-check without a normalising validator
    quietly changing a value the patient already approved."""
    once = validate_write(tool, PROPOSED[tool], context=CONTEXT)
    twice = validate_write(tool, once.values, context=CONTEXT)

    assert dict(twice.values) == dict(once.values)
    authority = write_authority(twice, patient_confirmed=True)
    assert authority.validated_arguments == VALIDATED[tool]
    assert isinstance(authority, WriteAuthority)
