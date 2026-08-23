"""Table-driven tests for the field validators every write passes through (TICK-061).

One table per field type, each carrying the shapes that field genuinely takes and the
shapes it does not, so a rule that quietly stops holding fails here rather than in a
chart. The observed real-world failure -- `"Update it to: 2002 Bridge Avenue"` reaching
a patient's record through TICK-050's address flow -- is a named regression case below.

These exercise the validators directly, independently of any tool call: the point of
TICK-061 is that the rules do not depend on what produced the value.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_server.llm.validation import (
    APPOINTMENT_REFUSAL,
    CITY_REFUSAL,
    DATE_OF_BIRTH_REFUSAL,
    NAME_REFUSAL,
    STATE_REFUSAL,
    STREET_REFUSAL,
    UNIT_REFUSAL,
    ZIP_REFUSAL,
    ValidationRefused,
    WriteContext,
    validate_assessment_answer,
    validate_birth_date,
    validate_mailing_address,
    validate_offered_appointment,
    validate_offered_slot,
    validate_person_name,
)
from ai_server.scheduling.appointments import AnonymousAppointmentToken
from ai_server.scheduling.slots import AnonymousSlotToken

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

VALID_ADDRESS = {
    "street1": "2002 Bridge Avenue",
    "city": "Point Pleasant",
    "state": "NJ",
    "zip_code": "08742",
}


def address(**overrides: object) -> dict[str, object]:
    return {**VALID_ADDRESS, **overrides}


# --- AC1/AC4: a street looks like a street -----------------------------------------

VALID_STREETS = (
    "2002 Bridge Avenue",
    "100 Maple Ave",
    "42 Oak St",
    "1 Main St",
    "200 Cedar Street",
    "9B Elm Street",
    "120-14 Queens Blvd",
    "PO Box 42",
    "P.O. Box 1170",
    "1234 W 3rd St Apt 2",
    "17 Martin Luther King Jr Boulevard",
)

INVALID_STREETS = (
    "Update it to 2002 Bridge Avenue",
    "2002 Bridge Avenue, is that right?",
    "2002 Bridge Avenue is that correct",
    "My address is 2002 Bridge Avenue",
    "Bridge Avenue",
    "2002",
    "Point Pleasant",
    "yes please",
    "I would like to change my address to 2002 Bridge Avenue",
    "2002 Bridge Avenue <script>alert(1)</script>",
    "",
    "   ",
    "x" * 101,
    123,
    None,
    {"street1": "2002 Bridge Avenue"},
)


def test_regression_tick_050_update_it_to_2002_bridge_avenue_is_refused_as_a_street() -> None:
    """REGRESSION (TICK-050, `docs/LOCAL_LLM_SPEC.md` "Why"): this exact string was
    written into a patient's chart as their street.

    `_parse_freeform_address` assigned the first comma-separated segment to `street1`
    unexamined, `validate_address` accepted any non-empty string, and the confirmation
    step rendered the parse rather than checking it. The street validator now refuses
    it, and would have refused it for the parser too -- it never looks at what produced
    the value.
    """
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(street1="Update it to: 2002 Bridge Avenue"))

    assert raised.value.patient_message == STREET_REFUSAL


@pytest.mark.parametrize("street", VALID_STREETS)
def test_a_street_line_is_accepted(street: str) -> None:
    assert validate_mailing_address(address(street1=street))["street1"] == street


@pytest.mark.parametrize("street", INVALID_STREETS)
def test_a_value_that_is_not_a_street_line_is_refused(street: object) -> None:
    with pytest.raises(ValidationRefused):
        validate_mailing_address(address(street1=street))


LEAD_INS = (
    "Update it to:",
    "Update it to",
    "Change my address to",
    "It is",
    "My new address is",
    "Please update it to",
)
TRAILERS = (", is that right?", " is that correct", ", thanks", " -- what do you think?")


@pytest.mark.parametrize("street", VALID_STREETS[:4])
@pytest.mark.parametrize("lead_in", LEAD_INS)
def test_no_lead_in_phrase_survives_the_street_validator(lead_in: str, street: str) -> None:
    """Property: for every street the validator accepts, prefixing conversation to it
    makes it unacceptable. A lead-in can never be carried through as part of a street."""
    with pytest.raises(ValidationRefused):
        validate_mailing_address(address(street1=f"{lead_in} {street}"))


@pytest.mark.parametrize("street", VALID_STREETS[:4])
@pytest.mark.parametrize("trailer", TRAILERS)
def test_no_trailing_question_survives_the_street_validator(trailer: str, street: str) -> None:
    """Property: the same holds for a question appended to an otherwise valid street."""
    with pytest.raises(ValidationRefused):
        validate_mailing_address(address(street1=f"{street}{trailer}"))


# --- AC1: the rest of an address ---------------------------------------------------


@pytest.mark.parametrize("unit", ["Apt 4B", "Suite 200", "Unit 12", "#3", "Floor 2"])
def test_a_unit_line_is_accepted(unit: str) -> None:
    assert validate_mailing_address(address(street2=unit))["street2"] == unit


@pytest.mark.parametrize("unit", ["", "   ", None])
def test_an_absent_unit_line_is_simply_absent(unit: object) -> None:
    assert "street2" not in validate_mailing_address(address(street2=unit))


@pytest.mark.parametrize("unit", ["is that right?", "Apt 4B, is that right", 123, "x" * 101])
def test_a_value_that_is_not_a_unit_line_is_refused(unit: object) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(street2=unit))
    assert raised.value.patient_message == UNIT_REFUSAL


@pytest.mark.parametrize(
    "city", ["Point Pleasant", "Springfield", "Reno", "Ho-Ho-Kus", "St. Louis"]
)
def test_a_town_or_city_name_is_accepted(city: str) -> None:
    assert validate_mailing_address(address(city=city))["city"] == city


@pytest.mark.parametrize(
    "city",
    [
        "Point Pleasant?",
        "Point Pleasant, NJ 08742",
        "08742",
        "is that right",
        "My city is Reno",
        "",
        "   ",
        123,
        None,
    ],
)
def test_a_value_that_is_not_a_town_or_city_is_refused(city: object) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(city=city))
    assert raised.value.patient_message == CITY_REFUSAL


@pytest.mark.parametrize("state", ["NJ", "nj", "Nj"])
def test_a_state_code_is_accepted_and_normalised(state: str) -> None:
    assert validate_mailing_address(address(state=state))["state"] == "NJ"


@pytest.mark.parametrize("state", ["New Jersey", "ZZ", "N", "", 123, None])
def test_a_value_that_is_not_a_state_code_is_refused(state: object) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(state=state))
    assert raised.value.patient_message == STATE_REFUSAL


@pytest.mark.parametrize("zip_code", ["08742", "08742-1234", "087421234"])
def test_a_zip_code_is_accepted(zip_code: str) -> None:
    assert validate_mailing_address(address(zip_code=zip_code))["zip_code"] == zip_code


@pytest.mark.parametrize("zip_code", ["8742", "not-a-zip", "08742 is that right", "", 123, None])
def test_a_value_that_is_not_a_zip_code_is_refused(zip_code: object) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(zip_code=zip_code))
    assert raised.value.patient_message == ZIP_REFUSAL


def test_an_address_is_refused_as_a_whole_and_returns_nothing_partial() -> None:
    """A single bad component refuses the address: there is no path that returns four
    good fields and drops the fifth."""
    with pytest.raises(ValidationRefused):
        validate_mailing_address(address(zip_code="nope"))


# --- AC1: a name looks like a name -------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Avery", "Avery Alden", "O'Neill", "Mary-Jane", "van der Berg", "St. John"]
)
def test_a_person_name_is_accepted(name: str) -> None:
    assert validate_person_name(name, label="given_name") == name


@pytest.mark.parametrize(
    "name",
    [
        "My name is Avery",
        "Update my name to Avery",
        "Avery 3rd",
        "Avery?",
        "It is Avery",
        "",
        "   ",
        "x" * 101,
        123,
        None,
    ],
)
def test_a_value_that_is_not_a_person_name_is_refused(name: object) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_person_name(name, label="given_name")
    assert raised.value.patient_message == NAME_REFUSAL


def test_a_name_is_whitespace_normalised_not_reinterpreted() -> None:
    assert validate_person_name("  Avery   Alden  ", label="given_name") == "Avery Alden"


# --- AC1: a date looks like a plausible date ---------------------------------------


@pytest.mark.parametrize("value", ["1990-01-01", "1985-04-01", "2008-08-23"])
def test_a_plausible_adult_date_of_birth_is_accepted(value: str) -> None:
    assert validate_birth_date(value, now=NOW) == value


@pytest.mark.parametrize(
    "value",
    [
        "2030-01-01",
        "2020-01-01",
        "1800-01-01",
        "1990-02-31",
        "01/01/1990",
        "not-a-date",
        "1990-01-01 is that right",
        "",
        123,
        None,
    ],
)
def test_a_value_that_is_not_a_plausible_date_of_birth_is_refused(value: object) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_birth_date(value, now=NOW)
    assert raised.value.patient_message == DATE_OF_BIRTH_REFUSAL


# --- AC1: an appointment reference resolves to one this patient actually has --------

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
    now=NOW,
    offered_appointments=(OFFERED_APPOINTMENT,),
    offered_slots=(OFFERED_SLOT,),
)


def test_an_appointment_the_patient_was_offered_resolves() -> None:
    resolved = validate_offered_appointment(OFFERED_APPOINTMENT.appointment_token, context=CONTEXT)
    assert resolved is OFFERED_APPOINTMENT


@pytest.mark.parametrize(
    "token",
    [
        "appt_someoneElsesAppointmentToken",
        "slot_gK3nQ7pR2mV8xT4bY6wZ1cJ5",
        "appt_gK3nQ7pR2mV8xT4bY6wZ1cJ6",
        "",
        123,
        None,
    ],
)
def test_an_appointment_reference_that_was_never_offered_is_refused(token: object) -> None:
    """A well-formed token is not a real one. Only what this patient's own session was
    issued resolves -- the model can imitate the pattern, not the issue."""
    with pytest.raises(ValidationRefused) as raised:
        validate_offered_appointment(token, context=CONTEXT)
    assert raised.value.patient_message == APPOINTMENT_REFUSAL


def test_an_appointment_reference_is_refused_when_nothing_was_offered() -> None:
    with pytest.raises(ValidationRefused):
        validate_offered_appointment(
            OFFERED_APPOINTMENT.appointment_token, context=WriteContext(now=NOW)
        )


def test_a_slot_the_patient_was_offered_resolves() -> None:
    assert validate_offered_slot(OFFERED_SLOT.slot_token, context=CONTEXT) is OFFERED_SLOT


@pytest.mark.parametrize("token", ["slot_invented", "appt_gK3nQ7pR2mV8xT4bY6wZ1cJ5", "", None])
def test_a_slot_reference_that_was_never_offered_is_refused(token: object) -> None:
    with pytest.raises(ValidationRefused):
        validate_offered_slot(token, context=CONTEXT)


# --- AC1: an assessment answer lands on a real choice or not at all -----------------

VALID_ANSWERS = (
    ("help_type", "both", "both"),
    ("help_type", "Counseling or therapy", "counseling_or_therapy"),
    ("help_type", "not-sure-yet", "not_sure_yet"),
    ("preferred_contact", "portal_message", "portal_message"),
    ("preferred_contact", "phone +15551234567", "phone +15551234567"),
    ("preferred_contact", "email avery@example.com", "email avery@example.com"),
    ("visit_preference", "video, weekday morning", "video,weekday_morning"),
    ("visit_preference", "in_person,weekend", "in_person,weekend"),
    ("accommodations", "language_interpreter", "language_interpreter"),
    ("accommodations", "none", "none"),
    (
        "accommodations",
        "language interpreter, mobility accommodation",
        "language_interpreter,mobility_accommodation",
    ),
)

INVALID_ANSWERS = (
    ("help_type", "I think maybe counselling would help"),
    ("help_type", "anxiety"),
    ("help_type", ""),
    ("help_type", None),
    ("preferred_contact", "phone"),
    ("preferred_contact", "phone 555-1234"),
    ("preferred_contact", "email not-an-email"),
    ("preferred_contact", "carrier pigeon"),
    ("visit_preference", "video"),
    ("visit_preference", "whenever, wherever"),
    ("accommodations", "an interpreter would be great"),
    ("accommodations", "language_interpreter, something_else"),
)


@pytest.mark.parametrize(("field_name", "answer", "canonical"), VALID_ANSWERS)
def test_an_answer_that_lands_on_a_real_choice_is_accepted(
    field_name: str, answer: str, canonical: str
) -> None:
    assert validate_assessment_answer(field_name, answer) == (field_name, canonical)


@pytest.mark.parametrize(("field_name", "answer"), INVALID_ANSWERS)
def test_an_answer_that_does_not_land_on_a_choice_is_refused(
    field_name: str, answer: object
) -> None:
    """Refused rather than mapped to the nearest choice: guessing which option a
    sentence meant is exactly the judgement this validator exists to withhold."""
    with pytest.raises(ValidationRefused):
        validate_assessment_answer(field_name, answer)


@pytest.mark.parametrize("field_name", ["social_security_number", "address", "", None, 7])
def test_an_answer_against_an_unknown_assessment_field_is_refused(field_name: object) -> None:
    with pytest.raises(ValidationRefused):
        validate_assessment_answer(field_name, "both")


# --- AC5: a refusal is legible, and never quotes what the model said ----------------

REFUSED_VALUES = (
    ("street1", "Update it to: 2002 Bridge Avenue"),
    ("street2", "is that the right apartment?"),
    ("city", "Point Pleasant, NJ 08742"),
    ("state", "Kalifornien"),
    ("zip_code", "not-a-zip"),
)

# The names only this codebase uses. A patient-facing sentence that contains one is
# describing the schema rather than the problem.
SCHEMA_INTERNALS = ("street1", "street2", "zip_code", "update_address", "tool", "validat")


@pytest.mark.parametrize(("component", "value"), REFUSED_VALUES)
def test_a_refusal_says_what_to_send_and_never_repeats_the_model_output(
    component: str, value: str
) -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(**{component: value}))

    message = raised.value.patient_message
    # Never an echo: what the model proposed is not shown back to the patient.
    assert value not in message
    assert "nothing was saved" in message
    assert "Please send" in message
    for internal in SCHEMA_INTERNALS:
        assert internal not in message.lower()


def test_a_refusal_keeps_its_developer_detail_off_the_patient_message() -> None:
    with pytest.raises(ValidationRefused) as raised:
        validate_mailing_address(address(street1="Update it to: 2002 Bridge Avenue"))

    assert raised.value.details == ["street1 must be a street line such as '100 Maple Ave'"]
    assert raised.value.details[0] not in raised.value.patient_message
