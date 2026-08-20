"""Field-by-field validation for the approved v1 assessment (`ONBOARDING_CONTRACT.md`,
"Field contract and flow order").

Every rule here matches either the contract table directly (identity fields 2-5,
validated only in the AI server since OpenEMR never sees an unconfirmed value) or the
OpenEMR-side validation already shipped in
`openemr_modules/aeai-portal-chat/src/Service/AssessmentDraftService.php` (draft
fields 6-9, kept in sync deliberately so a submission the AI server accepts is never
rejected by OpenEMR, and vice versa).

Each `validate_*` function raises `FieldValidationError` with one or more human-
readable messages; nothing here is fabricated or defaulted -- a missing or invalid
value always raises rather than silently substituting a value (mirrors
`openemr/demographics.py`'s `confirm_identity`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

FIELD_ORDER: tuple[str, ...] = (
    "given_name",
    "family_name",
    "date_of_birth",
    "address",
    "preferred_contact",
    "help_type",
    "visit_preference",
    "accommodations",
)

# Fields 6-9: the ones OpenEMR's assessment-draft endpoint itself stores and
# validates. Fields 2-5 (identity) are never checkpointed to the draft -- they are
# written directly to OpenEMR demographics only once confirmed at completion
# (ONBOARDING_CONTRACT.md "Draft and completion semantics" #5).
DRAFT_FIELDS: frozenset[str] = frozenset(
    {"preferred_contact", "help_type", "visit_preference", "accommodations"}
)
IDENTITY_FIELDS: frozenset[str] = frozenset(
    {"given_name", "family_name", "date_of_birth", "address"}
)

CONTACT_METHODS: tuple[str, ...] = ("phone", "email", "portal_message")
HELP_TYPES: tuple[str, ...] = (
    "counseling_or_therapy",
    "psychiatric_evaluation_or_medication_support",
    "both",
    "not_sure_yet",
)
VISIT_FORMATS: tuple[str, ...] = ("in_person", "video", "either", "not_sure")
VISIT_TIME_WINDOWS: tuple[str, ...] = (
    "weekday_morning",
    "weekday_afternoon",
    "weekday_evening",
    "weekend",
    "no_preference",
)
ACCOMMODATIONS: tuple[str, ...] = (
    "language_interpreter",
    "hearing_accommodation",
    "vision_accommodation",
    "mobility_accommodation",
    "other_accommodation",
)

# USPS two-letter state and territory codes; ONBOARDING_CONTRACT.md row 5 requires
# "two-letter US state/territory" without enumerating the set, so this uses USPS's
# own published list (50 states, DC, and the inhabited territories).
US_STATE_AND_TERRITORY_CODES: frozenset[str] = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
        "AS",
        "GU",
        "MP",
        "PR",
        "VI",
    }
)

_MIN_ADULT_AGE_YEARS = 18
_MAX_TEXT_FIELD_LENGTH = 100
_MAX_ACCOMMODATION_DETAIL_LENGTH = 200
_PHONE_PATTERN = re.compile(r"^\+1\d{10}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZIP_PATTERN = re.compile(r"^\d{5}(-?\d{4})?$")


class FieldValidationError(Exception):
    """Raised when a submitted field fails `ONBOARDING_CONTRACT.md` validation.

    `details` holds every individual message (there can be more than one for a
    compound field, e.g. address); `str(exc)` is always at least the first of them.
    """

    def __init__(self, details: list[str]) -> None:
        if not details:
            raise ValueError("FieldValidationError requires at least one detail message")
        super().__init__(details[0])
        self.details = details


@dataclass(frozen=True)
class Address:
    """A validated mailing address (`ONBOARDING_CONTRACT.md` row 5)."""

    street1: str
    city: str
    state: str
    zip_code: str
    street2: str | None = None


def validate_text_name(value: object, *, label: str) -> str:
    """Validate a legal given/family name: 1-100 non-whitespace characters."""
    if not isinstance(value, str):
        raise FieldValidationError([f"{label} must be text"])
    stripped = value.strip()
    if not stripped or len(stripped) > _MAX_TEXT_FIELD_LENGTH:
        raise FieldValidationError([f"{label} must be 1-100 non-whitespace characters"])
    return stripped


def validate_date_of_birth(value: object, *, now: datetime) -> str:
    """Validate a calendar date that is not in the future and is 18+ as of `now`."""
    if not isinstance(value, str):
        raise FieldValidationError(["date_of_birth must be a date string"])
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FieldValidationError(["date_of_birth must be a valid calendar date"]) from exc
    today = now.date()
    if parsed > today:
        raise FieldValidationError(["date_of_birth must not be in the future"])
    age = today.year - parsed.year - ((today.month, today.day) < (parsed.month, parsed.day))
    if age < _MIN_ADULT_AGE_YEARS:
        raise FieldValidationError(
            ["date_of_birth shows the patient is under 18; this demo flow requires an adult"]
        )
    return parsed.isoformat()


def validate_address(value: object) -> Address:
    """Validate street line 1, city, two-letter state/territory, and ZIP."""
    if not isinstance(value, dict):
        raise FieldValidationError(["address must be an object"])
    errors: list[str] = []

    street1 = value.get("street1")
    if not isinstance(street1, str) or not street1.strip():
        errors.append("street1 is required")
        street1 = ""

    street2 = value.get("street2")
    if street2 is not None and not isinstance(street2, str):
        errors.append("street2 must be text if provided")
        street2 = None

    city = value.get("city")
    if not isinstance(city, str) or not city.strip():
        errors.append("city is required")
        city = ""

    state = value.get("state")
    if not isinstance(state, str) or state.upper() not in US_STATE_AND_TERRITORY_CODES:
        errors.append("state must be a two-letter US state or territory code")
        state = ""

    zip_code = value.get("zip_code")
    if not isinstance(zip_code, str) or not _ZIP_PATTERN.match(zip_code):
        errors.append("zip_code must be a five- or nine-digit US ZIP code")
        zip_code = ""

    if errors:
        raise FieldValidationError(errors)
    return Address(
        street1=street1.strip(),
        city=city.strip(),
        state=state.upper(),
        zip_code=zip_code,
        street2=street2.strip() if street2 else None,
    )


def validate_preferred_contact(value: object) -> dict[str, str]:
    """Validate contact method and, when required, its syntactically valid value.

    Mirrors `AssessmentDraftService::validatedFields` exactly, including its
    deliberate omission of `contact_value` for `method=portal_message` (never
    required, never stored -- there is nothing to validate or return).
    """
    if not isinstance(value, dict):
        raise FieldValidationError(["preferred_contact must be an object"])
    method = value.get("method")
    if method not in CONTACT_METHODS:
        raise FieldValidationError([f"method must be one of: {', '.join(CONTACT_METHODS)}"])
    if method == "portal_message":
        return {"preferred_contact_method": method}

    contact_value = value.get("value")
    if not isinstance(contact_value, str) or not contact_value:
        raise FieldValidationError([f"value is required (a {method} contact value)"])
    if method == "phone" and not _PHONE_PATTERN.match(contact_value):
        raise FieldValidationError(["value must be an E.164 US phone number for method=phone"])
    if method == "email" and not _EMAIL_PATTERN.match(contact_value):
        raise FieldValidationError(["value must be a valid email address for method=email"])
    return {"preferred_contact_method": method, "contact_value": contact_value}


def validate_help_type(value: object) -> dict[str, str]:
    """Validate the single required help-type selection."""
    if value not in HELP_TYPES:
        raise FieldValidationError([f"help_type must be one of: {', '.join(HELP_TYPES)}"])
    return {"help_type": value}


def validate_visit_preference(value: object) -> dict[str, str]:
    """Validate the required visit format and time-window selections."""
    if not isinstance(value, dict):
        raise FieldValidationError(["visit_preference must be an object"])
    errors: list[str] = []
    visit_format = value.get("format")
    if visit_format not in VISIT_FORMATS:
        errors.append(f"format must be one of: {', '.join(VISIT_FORMATS)}")
    time_window = value.get("time_window")
    if time_window not in VISIT_TIME_WINDOWS:
        errors.append(f"time_window must be one of: {', '.join(VISIT_TIME_WINDOWS)}")
    if errors:
        raise FieldValidationError(errors)
    return {"visit_format": visit_format, "visit_time_window": time_window}


def validate_accommodations(value: object) -> dict[str, object]:
    """Validate the optional multi-select and its optional, bounded free-text detail.

    `accommodation_detail` is only ever required to be valid -- never required to be
    present -- even when `other_accommodation` is selected
    (`ONBOARDING_CONTRACT.md` row 9), matching the OpenEMR-side fix recorded in
    `evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md`.
    """
    if not isinstance(value, dict):
        raise FieldValidationError(["accommodations must be an object"])
    selected = value.get("selected", [])
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        raise FieldValidationError(["selected must be an array of accommodation codes"])
    invalid = sorted(set(selected) - set(ACCOMMODATIONS))
    if invalid:
        raise FieldValidationError([f"selected may only contain: {', '.join(ACCOMMODATIONS)}"])
    result: dict[str, object] = {"accommodations": list(selected)}
    detail = value.get("detail")
    if "other_accommodation" in selected and detail is not None:
        if not isinstance(detail, str) or len(detail) > _MAX_ACCOMMODATION_DETAIL_LENGTH:
            raise FieldValidationError(
                [f"detail must be {_MAX_ACCOMMODATION_DETAIL_LENGTH} characters or fewer"]
            )
        result["accommodation_detail"] = detail
    return result


def validate_field(field: str, value: object, *, now: datetime) -> object:
    """Dispatch to the validator for `field`; raises `FieldValidationError` on any issue.

    Returns the OpenEMR-shaped dict (`{key: value, ...}`) for a draft field, or the
    validated scalar/`Address` for an identity field -- exactly what
    `onboarding/flow.py` needs to either checkpoint or hold for completion.
    """
    if field == "given_name":
        return validate_text_name(value, label="given_name")
    if field == "family_name":
        return validate_text_name(value, label="family_name")
    if field == "date_of_birth":
        return validate_date_of_birth(value, now=now)
    if field == "address":
        return validate_address(value)
    if field == "preferred_contact":
        return validate_preferred_contact(value)
    if field == "help_type":
        return validate_help_type(value)
    if field == "visit_preference":
        return validate_visit_preference(value)
    if field == "accommodations":
        return validate_accommodations(value)
    raise FieldValidationError([f"unknown field: {field}"])
