"""Contract-fixture tests for `ai_server/onboarding/fields.py` (TICK-017, AC1).

Every case in `ONBOARDING_CONTRACT.md`'s "Fixture cases" table that concerns field
validation is represented here, plus the exact enum/pattern values
`AssessmentDraftService.php` accepts, so an AI-server-accepted submission is never
one OpenEMR itself would reject.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_server.onboarding.fields import (
    ACCOMMODATIONS,
    CONTACT_METHODS,
    HELP_TYPES,
    VISIT_FORMATS,
    VISIT_TIME_WINDOWS,
    Address,
    FieldValidationError,
    validate_accommodations,
    validate_address,
    validate_date_of_birth,
    validate_field,
    validate_help_type,
    validate_preferred_contact,
    validate_text_name,
    validate_visit_preference,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def test_ac1_a_valid_name_is_stripped_and_accepted() -> None:
    assert validate_text_name("  Avery  ", label="given_name") == "Avery"


@pytest.mark.parametrize("value", ["", "   ", "x" * 101, 123, None])
def test_ac1_an_invalid_name_is_rejected(value: object) -> None:
    with pytest.raises(FieldValidationError):
        validate_text_name(value, label="given_name")


def test_ac1_a_valid_adult_date_of_birth_is_accepted() -> None:
    assert validate_date_of_birth("1990-01-01", now=NOW) == "1990-01-01"


def test_ac1_a_date_of_birth_turning_18_today_is_accepted() -> None:
    assert validate_date_of_birth("2008-08-20", now=NOW) == "2008-08-20"


def test_fixture_under_18_date_of_birth_is_rejected() -> None:
    """`ONBOARDING_CONTRACT.md` fixture: "User under 18" blocks the adult-demo flow."""
    with pytest.raises(FieldValidationError, match="under 18"):
        validate_date_of_birth("2008-08-21", now=NOW)


def test_ac1_a_future_date_of_birth_is_rejected() -> None:
    with pytest.raises(FieldValidationError, match="future"):
        validate_date_of_birth("2027-01-01", now=NOW)


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-40", 20080820, None])
def test_ac1_a_malformed_date_of_birth_is_rejected(value: object) -> None:
    with pytest.raises(FieldValidationError):
        validate_date_of_birth(value, now=NOW)


def test_ac1_a_valid_address_is_accepted() -> None:
    address = validate_address(
        {
            "street1": "100 Maple Avenue",
            "street2": "Apt 4",
            "city": "Springfield",
            "state": "il",
            "zip_code": "62704",
        }
    )
    assert address == Address(
        street1="100 Maple Avenue",
        street2="Apt 4",
        city="Springfield",
        state="IL",
        zip_code="62704",
    )


def test_ac1_an_address_without_street2_is_accepted() -> None:
    address = validate_address(
        {"street1": "1 Main St", "city": "Reno", "state": "NV", "zip_code": "89501-1234"}
    )
    assert address.street2 is None
    assert address.zip_code == "89501-1234"


@pytest.mark.parametrize(
    "value",
    [
        {"street1": "", "city": "Reno", "state": "NV", "zip_code": "89501"},
        {"street1": "1 Main St", "city": "", "state": "NV", "zip_code": "89501"},
        {"street1": "1 Main St", "city": "Reno", "state": "ZZ", "zip_code": "89501"},
        {"street1": "1 Main St", "city": "Reno", "state": "NV", "zip_code": "890"},
        {"street1": "1 Main St", "city": "Reno", "state": "NV", "zip_code": "not-a-zip"},
        "not-an-object",
        None,
    ],
)
def test_fixture_invalid_address_is_rejected(value: object) -> None:
    """`ONBOARDING_CONTRACT.md` fixture: "Invalid date, address, phone, or email"."""
    with pytest.raises(FieldValidationError):
        validate_address(value)


def test_ac1_a_valid_phone_contact_is_accepted() -> None:
    assert validate_preferred_contact({"method": "phone", "value": "+15551234567"}) == {
        "preferred_contact_method": "phone",
        "contact_value": "+15551234567",
    }


def test_ac1_a_valid_email_contact_is_accepted() -> None:
    assert validate_preferred_contact({"method": "email", "value": "avery@example.com"}) == {
        "preferred_contact_method": "email",
        "contact_value": "avery@example.com",
    }


def test_fixture_portal_message_needs_no_contact_value() -> None:
    """`ONBOARDING_CONTRACT.md` fixture: "Contact preference is portal message"."""
    assert validate_preferred_contact({"method": "portal_message"}) == {
        "preferred_contact_method": "portal_message"
    }
    # Even if a stray value is supplied, it is never required or returned, mirroring
    # AssessmentDraftService.php's own silent-drop behavior for this method.
    assert validate_preferred_contact({"method": "portal_message", "value": "ignored"}) == {
        "preferred_contact_method": "portal_message"
    }


@pytest.mark.parametrize(
    "value",
    [
        {"method": "phone", "value": "555-123-4567"},
        {"method": "phone", "value": "+445551234567"},
        {"method": "phone"},
        {"method": "email", "value": "not-an-email"},
        {"method": "email"},
        {"method": "carrier_pigeon", "value": "x"},
        {},
        "not-an-object",
    ],
)
def test_fixture_invalid_phone_or_email_is_rejected(value: object) -> None:
    with pytest.raises(FieldValidationError):
        validate_preferred_contact(value)


@pytest.mark.parametrize("value", HELP_TYPES)
def test_ac1_every_approved_help_type_is_accepted(value: str) -> None:
    assert validate_help_type(value) == {"help_type": value}


def test_ac1_an_unapproved_help_type_is_rejected() -> None:
    with pytest.raises(FieldValidationError):
        validate_help_type("something_else")


@pytest.mark.parametrize("visit_format", VISIT_FORMATS)
@pytest.mark.parametrize("time_window", VISIT_TIME_WINDOWS)
def test_ac1_every_approved_visit_preference_combination_is_accepted(
    visit_format: str, time_window: str
) -> None:
    assert validate_visit_preference({"format": visit_format, "time_window": time_window}) == {
        "visit_format": visit_format,
        "visit_time_window": time_window,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"format": "levitation", "time_window": "weekend"},
        {"format": "video", "time_window": "sometime"},
        {"format": "video"},
        {"time_window": "weekend"},
    ],
)
def test_ac1_an_invalid_visit_preference_is_rejected(value: object) -> None:
    with pytest.raises(FieldValidationError):
        validate_visit_preference(value)


def test_fixture_other_accommodation_detail_is_optional() -> None:
    """`ONBOARDING_CONTRACT.md` fixture: "Other accommodation selected"."""
    assert validate_accommodations({"selected": ["other_accommodation"]}) == {
        "accommodations": ["other_accommodation"]
    }
    assert validate_accommodations(
        {"selected": ["other_accommodation"], "detail": "wheelchair access"}
    ) == {
        "accommodations": ["other_accommodation"],
        "accommodation_detail": "wheelchair access",
    }


def test_ac1_accommodations_may_be_empty() -> None:
    assert validate_accommodations({"selected": []}) == {"accommodations": []}


def test_ac1_a_detail_without_other_accommodation_is_dropped() -> None:
    """Mirrors `AssessmentDraftService.php`: detail is only stored alongside the
    `other_accommodation` selection, never on its own."""
    result = validate_accommodations(
        {"selected": ["language_interpreter"], "detail": "should not be kept"}
    )
    assert result == {"accommodations": ["language_interpreter"]}


def test_ac1_a_detail_over_200_characters_is_rejected() -> None:
    with pytest.raises(FieldValidationError, match="200"):
        validate_accommodations({"selected": ["other_accommodation"], "detail": "x" * 201})


@pytest.mark.parametrize(
    "value",
    [
        {"selected": ["not_a_real_option"]},
        {"selected": "language_interpreter"},
        {"selected": [1, 2]},
        "not-an-object",
    ],
)
def test_ac1_invalid_accommodations_are_rejected(value: object) -> None:
    with pytest.raises(FieldValidationError):
        validate_accommodations(value)


def test_ac1_every_accommodation_enum_is_individually_accepted() -> None:
    for option in ACCOMMODATIONS:
        assert validate_accommodations({"selected": [option]})["accommodations"] == [option]


def test_ac1_every_contact_method_enum_is_a_recognized_value() -> None:
    assert set(CONTACT_METHODS) == {"phone", "email", "portal_message"}


def test_ac1_validate_field_dispatches_every_known_field() -> None:
    assert validate_field("given_name", "Avery", now=NOW) == "Avery"
    assert validate_field("family_name", "Alden", now=NOW) == "Alden"
    assert validate_field("date_of_birth", "1990-01-01", now=NOW) == "1990-01-01"
    assert isinstance(
        validate_field(
            "address",
            {"street1": "1 Main St", "city": "Reno", "state": "NV", "zip_code": "89501"},
            now=NOW,
        ),
        Address,
    )
    assert validate_field("help_type", "both", now=NOW) == {"help_type": "both"}


def test_ac1_validate_field_rejects_an_unknown_field_name() -> None:
    with pytest.raises(FieldValidationError, match="unknown field"):
        validate_field("favorite_color", "blue", now=NOW)
