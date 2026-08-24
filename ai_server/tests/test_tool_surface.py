"""Tests for TICK-060: the schema-constrained tool surface the model may call.

Sectioned by the ticket's acceptance criteria. The recurring shape is `_dispatch()`
below: it composes the two public entry points in the order a caller must use them and
records what would have reached a service, so every refusal test can assert not merely
that an exception was raised but that nothing was executed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from ai_server.llm.tools import (
    APPOINTMENT_TOKEN_PATTERN,
    SLOT_TOKEN_PATTERN,
    TOOL_CALL_REFUSED_RESPONSE,
    TOOL_NAMES,
    TOOL_SURFACE,
    UNCONFIRMED_WRITE_RESPONSE,
    UNKNOWN_TOOL_RESPONSE,
    UPLOAD_ID_PATTERN,
    MalformedToolCallError,
    MultipleToolCallsError,
    NoToolCallError,
    ToolSurfaceError,
    UnknownToolError,
    WriteAuthority,
    WriteAuthorityError,
    executable_arguments,
    parse_tool_call,
    tool_call_json_schema,
    tool_definition,
)
from ai_server.ocr.service import OcrService, OcrUploadStore
from ai_server.openemr.adapter import Appointment
from ai_server.scheduling.appointments import AnonymousAppointmentStore, AppointmentTokenError
from ai_server.scheduling.slots import AnonymousSlotStore, CandidateSlot, SlotTokenError

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
PATIENT_ID = "41"

# One well-formed call per published tool, and one malformed variant of each. The
# malformed variant always breaks the tool's own schema rather than the envelope, so
# each row exercises that tool's contract and not just the shared one.
WELL_FORMED = {
    "update_address": {
        "street1": "2002 Bridge Avenue",
        "street2": None,
        "city": "Point Pleasant",
        "state": "NJ",
        "zip_code": "08742",
    },
    "update_demographics": {"given_name": "Dana", "family_name": None, "date_of_birth": None},
    "record_assessment_answer": {"field": "help_type", "answer": "anxiety"},
    "list_appointments": {},
    "find_slots": {},
    "book_appointment": {"slot_token": "slot_gK3nQ7pR2mV8xT4bY6wZ1cJ5"},
    "cancel_appointment": {"appointment_token": "appt_gK3nQ7pR2mV8xT4bY6wZ1cJ5"},
    "extract_document_fields": {"upload_id": "gK3nQ7pR2mV8xT4bY6wZ1cJ5"},
    "ask_general_knowledge": {"restatement": "What is a deductible?"},
    "reply": {"message": "Your appointment is on Tuesday."},
}

MALFORMED = {
    "update_address": {"street1": "2002 Bridge Avenue"},
    "update_demographics": {},
    "record_assessment_answer": {"field": "social_security_number", "answer": "anxiety"},
    "list_appointments": {"patient_id": "41"},
    "find_slots": {"provider_id": "7"},
    "book_appointment": {"slot_token": "12"},
    "cancel_appointment": {"appointment_token": "slot_gK3nQ7pR2mV8xT4bY6wZ1cJ5"},
    "extract_document_fields": {"upload_id": "../etc/passwd"},
    "ask_general_knowledge": {"restatement": ""},
    "reply": {"message": ""},
}


def call_json(tool: str, arguments: dict) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


def authority_for(tool: str) -> WriteAuthority:
    """The evidence TICK-061's validation plus the confirmation step will produce."""
    return WriteAuthority(
        tool=tool, validated_arguments={"validated": True}, patient_confirmed=True
    )


def _dispatch(raw: str, executed: list, *, authority: WriteAuthority | None = None):
    """Parse then authorise, in the only order a caller may use, recording what would
    actually have reached a backing service."""
    call = parse_tool_call(raw)
    arguments = executable_arguments(call, authority=authority)
    executed.append((call.tool, arguments))
    return call


# --- AC1: every tool has a strict schema; a malformed call is refused before execution ---


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_a_well_formed_call_to_each_tool_is_accepted_and_executes(tool: str) -> None:
    executed: list = []
    authority = authority_for(tool) if TOOL_SURFACE[tool].writes else None
    call = _dispatch(call_json(tool, WELL_FORMED[tool]), executed, authority=authority)
    assert call.tool == tool
    assert isinstance(call.arguments, TOOL_SURFACE[tool].arguments)
    assert [name for name, _ in executed] == [tool]


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_a_malformed_call_to_each_tool_is_refused_without_side_effects(tool: str) -> None:
    executed: list = []
    with pytest.raises(MalformedToolCallError) as raised:
        _dispatch(call_json(tool, MALFORMED[tool]), executed, authority=authority_for(tool))
    assert executed == []
    assert raised.value.details
    assert raised.value.patient_message == TOOL_CALL_REFUSED_RESPONSE


def test_every_published_tool_has_a_closed_argument_schema() -> None:
    """`extra="forbid"` is what makes an argument the model invented a refusal rather
    than a value quietly carried past validation."""
    for name in TOOL_NAMES:
        definition = TOOL_SURFACE[name]
        assert issubclass(definition.arguments, BaseModel)
        assert definition.arguments.model_config["extra"] == "forbid"
        assert definition.call.model_config["extra"] == "forbid"


def test_the_constrained_decoding_schema_admits_exactly_the_published_surface() -> None:
    """Handed to a runtime's structured-output mode, this schema is what makes a
    malformed call unsampleable rather than merely rejected afterwards."""
    schema = tool_call_json_schema()
    branches = schema.get("oneOf") or schema.get("anyOf")
    assert branches is not None and len(branches) == len(TOOL_NAMES)
    definitions = schema["$defs"]
    named = set()
    for branch in branches:
        call_schema = definitions[branch["$ref"].rsplit("/", 1)[-1]]
        assert call_schema["additionalProperties"] is False
        assert sorted(call_schema["required"]) == ["arguments", "tool"]
        named.add(call_schema["properties"]["tool"]["const"])
    assert named == set(TOOL_NAMES)
    # The discriminator is what stops one tool's arguments being accepted under
    # another tool's name.
    assert schema["discriminator"]["propertyName"] == "tool"


def test_a_partially_valid_call_hands_no_field_onward() -> None:
    """A call whose first field is fine and whose second is not must not be applied in
    part -- the address bug this whole spec exists to end was a partial write."""
    executed: list = []
    with pytest.raises(MalformedToolCallError):
        _dispatch(
            call_json("update_address", {"street1": "2002 Bridge Avenue", "city": ""}),
            executed,
            authority=authority_for("update_address"),
        )
    assert executed == []


# --- AC2: exactly one tool call per turn ---


def test_a_turn_with_no_tool_call_is_a_defined_refusal() -> None:
    executed: list = []
    for empty in ("", "   ", "[]"):
        with pytest.raises(NoToolCallError) as raised:
            _dispatch(empty, executed)
        assert raised.value.patient_message == TOOL_CALL_REFUSED_RESPONSE
    assert executed == []


def test_a_turn_with_more_than_one_tool_call_is_refused_rather_than_narrowed() -> None:
    """Neither call runs. Taking the first would execute an action the model expressed
    only half a commitment to."""
    executed: list = []
    both = [
        json.loads(call_json("cancel_appointment", WELL_FORMED["cancel_appointment"])),
        json.loads(call_json("reply", WELL_FORMED["reply"])),
    ]
    with pytest.raises(MultipleToolCallsError):
        _dispatch(json.dumps(both), executed, authority=authority_for("cancel_appointment"))
    concatenated = call_json("reply", WELL_FORMED["reply"]) + call_json(
        "reply", WELL_FORMED["reply"]
    )
    with pytest.raises(MultipleToolCallsError):
        _dispatch(concatenated, executed)
    assert executed == []


def test_a_single_call_wrapped_in_an_array_is_still_one_call() -> None:
    executed: list = []
    wrapped = json.dumps([json.loads(call_json("reply", WELL_FORMED["reply"]))])
    _dispatch(wrapped, executed)
    assert [name for name, _ in executed] == ["reply"]


def test_every_defined_refusal_carries_patient_visible_behaviour() -> None:
    """None of these may reach the transcript as an exception; each is a sentence the
    chat can answer with."""
    refusals = (
        "",
        json.dumps([{"tool": "reply"}, {"tool": "reply"}]),
        call_json("teleport_patient", {}),
        call_json("reply", {"message": ""}),
        "not json at all",
    )
    for raw in refusals:
        with pytest.raises(ToolSurfaceError) as raised:
            parse_tool_call(raw)
        assert raised.value.patient_message
        assert raised.value.patient_message[0].isupper()


# --- AC3: every tool declares whether it writes, and writing needs more than a schema ---


def test_every_tool_declares_whether_it_writes() -> None:
    declared = {name: TOOL_SURFACE[name].writes for name in TOOL_NAMES}
    assert declared == {
        "update_address": True,
        "update_demographics": True,
        "record_assessment_answer": True,
        "list_appointments": False,
        "find_slots": False,
        "book_appointment": True,
        "cancel_appointment": True,
        "extract_document_fields": False,
        "ask_general_knowledge": False,
        "reply": False,
    }


@pytest.mark.parametrize("tool", sorted(name for name in TOOL_NAMES if TOOL_SURFACE[name].writes))
def test_no_writing_tool_executes_on_a_schema_valid_call_alone(tool: str) -> None:
    """A perfectly well-formed call is still not authority to write."""
    executed: list = []
    with pytest.raises(WriteAuthorityError) as raised:
        _dispatch(call_json(tool, WELL_FORMED[tool]), executed)
    assert executed == []
    assert raised.value.patient_message == UNCONFIRMED_WRITE_RESPONSE


@pytest.mark.parametrize("tool", sorted(name for name in TOOL_NAMES if TOOL_SURFACE[name].writes))
def test_a_writing_tool_executes_on_validated_values_not_the_models_own(tool: str) -> None:
    """What reaches the service is TICK-061's validated output, never the raw argument
    values the model proposed."""
    executed: list = []
    _dispatch(call_json(tool, WELL_FORMED[tool]), executed, authority=authority_for(tool))
    ((_, arguments),) = executed
    assert arguments == {"validated": True}
    assert arguments != WELL_FORMED[tool]


@pytest.mark.parametrize("tool", sorted(name for name in TOOL_NAMES if TOOL_SURFACE[name].writes))
def test_an_unconfirmed_or_mismatched_authority_cannot_write(tool: str) -> None:
    executed: list = []
    unconfirmed = WriteAuthority(
        tool=tool, validated_arguments={"validated": True}, patient_confirmed=False
    )
    with pytest.raises(WriteAuthorityError):
        _dispatch(call_json(tool, WELL_FORMED[tool]), executed, authority=unconfirmed)
    borrowed = authority_for("reply")
    with pytest.raises(WriteAuthorityError):
        _dispatch(call_json(tool, WELL_FORMED[tool]), executed, authority=borrowed)
    assert executed == []


def test_a_read_only_tool_executes_on_its_own_validated_arguments() -> None:
    executed: list = []
    _dispatch(call_json("find_slots", {}), executed)
    _dispatch(call_json("ask_general_knowledge", WELL_FORMED["ask_general_knowledge"]), executed)
    assert executed == [
        ("find_slots", {}),
        ("ask_general_knowledge", {"restatement": "What is a deductible?"}),
    ]


# --- AC4: an invented tool name is refused and reported, never silently ignored ---


def test_an_invented_tool_name_is_refused_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    executed: list = []
    with caplog.at_level("WARNING", logger="ai_server.llm.tools"):
        with pytest.raises(UnknownToolError) as raised:
            _dispatch(
                call_json("reschedule_appointment", {"appointment_token": "appt_x"}), executed
            )
    assert executed == []
    assert raised.value.tool_name == "reschedule_appointment"
    # Reported, not silent: a hallucinated capability that no-ops is indistinguishable
    # to the patient from one that worked.
    assert "reschedule_appointment" in caplog.text
    assert raised.value.patient_message == UNKNOWN_TOOL_RESPONSE
    assert raised.value.patient_message != TOOL_CALL_REFUSED_RESPONSE


def test_reschedule_is_not_on_the_surface() -> None:
    """No OpenEMR service method exists for it (TICK-020), and a tool the system cannot
    honour is a promise the model is free to make on its own judgement."""
    assert not any("reschedule" in name for name in TOOL_NAMES)
    assert "RescheduleService" not in {definition.backed_by for definition in TOOL_SURFACE.values()}


def test_a_tool_name_that_is_not_a_name_is_refused_without_reaching_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The value is model output, so it is not assumed to be a tool name at all."""
    executed: list = []
    with caplog.at_level("WARNING", logger="ai_server.llm.tools"):
        for name in ("Dana Whitfield, 12 Ocean Road", 7, None):
            with pytest.raises(UnknownToolError):
                _dispatch(json.dumps({"tool": name, "arguments": {}}), executed)
    assert executed == []
    assert "Ocean Road" not in caplog.text
    assert "<unprintable>" in caplog.text


def test_tool_definition_refuses_an_unpublished_name() -> None:
    with pytest.raises(UnknownToolError):
        tool_definition("update_insurance")


# --- AC5: no OpenEMR identifier is ever a tool argument ---


def test_no_argument_on_the_surface_names_an_openemr_record() -> None:
    forbidden = (
        "pid",
        "uuid",
        "patient_id",
        "appointment_id",
        "slot_id",
        "event_id",
        "encounter",
        "provider_id",
        "facility_id",
        "billing_location_id",
        "category_id",
    )
    for name in TOOL_NAMES:
        for field in TOOL_SURFACE[name].arguments.model_fields:
            assert field not in forbidden, f"{name}.{field}"


def test_the_only_references_on_the_surface_are_anonymous_tokens() -> None:
    """Every field that points at something rather than describing it is a token some
    local store minted, and those stores are the only resolvers."""
    references = {
        "book_appointment": ("slot_token", SLOT_TOKEN_PATTERN),
        "cancel_appointment": ("appointment_token", APPOINTMENT_TOKEN_PATTERN),
        "extract_document_fields": ("upload_id", UPLOAD_ID_PATTERN),
    }
    for tool, (field, pattern) in references.items():
        arguments = TOOL_SURFACE[tool].arguments
        assert tuple(arguments.model_fields) == (field,)
        assert arguments.model_json_schema()["properties"][field]["pattern"] == pattern


# `test_the_token_patterns_match_the_ones_already_on_the_wire` stood here. It guarded
# this module's regexes against the second copy in `privacy/gate.py`'s `AnonymousSlot`/
# `AnonymousAppointment`. TICK-064 deleted those with the rest of the outbound scheduling
# context (D13), so there is no second copy left to drift from and nothing for the guard
# to compare. The patterns are asserted against real issued tokens in
# `test_slot_discovery.py` and `test_appointment_tokens.py` instead.


def test_a_fabricated_appointment_token_cannot_reach_the_cancellation_service() -> None:
    """The schema accepts the shape; the store is what refuses the reference."""
    store = AnonymousAppointmentStore()
    real = store.issue(
        Appointment(
            id="9001",
            status="booked",
            start=NOW + timedelta(days=1),
            end=NOW + timedelta(days=1, minutes=30),
        ),
        PATIENT_ID,
        NOW,
    )
    fabricated = "appt_" + "f" * 24
    call = parse_tool_call(call_json("cancel_appointment", {"appointment_token": fabricated}))
    assert call.arguments.appointment_token == fabricated
    with pytest.raises(AppointmentTokenError):
        store.resolve(fabricated, PATIENT_ID, NOW)
    # A token that was genuinely issued still resolves, so the refusal above is the
    # fabrication being caught and not the store refusing everything.
    assert store.resolve(real.appointment_token, PATIENT_ID, NOW) == "9001"


def test_a_fabricated_slot_token_cannot_reach_the_booking_service() -> None:
    store = AnonymousSlotStore()
    issued = store.issue(
        CandidateSlot(
            starts_at=NOW + timedelta(days=2), ends_at=NOW + timedelta(days=2, minutes=30)
        ),
        NOW,
    )
    fabricated = "slot_" + "f" * 24
    call = parse_tool_call(call_json("book_appointment", {"slot_token": fabricated}))
    assert call.arguments.slot_token == fabricated
    with pytest.raises(SlotTokenError):
        store.resolve(fabricated, NOW)
    assert store.resolve(issued.slot_token, NOW).starts_at == NOW + timedelta(days=2)


def test_a_real_upload_handle_satisfies_the_documented_pattern() -> None:
    """The OCR handle is bounded by what `OcrService.begin()` actually mints."""
    service = OcrService(store=OcrUploadStore(), engine=_UnusedEngine())
    upload_id = service.begin(consent=True, now=NOW)
    call = parse_tool_call(call_json("extract_document_fields", {"upload_id": upload_id}))
    assert call.arguments.upload_id == upload_id


class _UnusedEngine:
    """`begin()` mints a handle without extracting; no engine call should happen."""

    def extract(self, image: bytes) -> str:
        raise AssertionError("the OCR engine must not run while minting a handle")


# --- AC6: the surface is documented where it is defined ---


def test_the_surface_documents_itself_including_the_reschedule_omission() -> None:
    import ai_server.llm.tools as module

    documentation = module.__doc__ or ""
    for name in TOOL_NAMES:
        assert f"`{name}`" in documentation
        assert TOOL_SURFACE[name].backed_by in documentation or name == "reply"
    assert "RescheduleService" in documentation
    assert "TICK-020" in documentation


def test_every_tool_names_the_service_that_actually_runs_it() -> None:
    assert {name: TOOL_SURFACE[name].backed_by for name in TOOL_NAMES} == {
        "update_address": "PatientDemographicsUpdateService",
        "update_demographics": "PatientDemographicsUpdateService",
        "record_assessment_answer": "AssessmentDraftService",
        "list_appointments": "AppointmentDiscoveryService",
        "find_slots": "SlotDiscoveryService",
        "book_appointment": "BookingService",
        "cancel_appointment": "CancellationService",
        "extract_document_fields": "OcrService",
        "ask_general_knowledge": "Groq",
        "reply": "none",
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
