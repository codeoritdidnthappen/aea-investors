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

TICK-061 made the free-text identity rules (street, unit, city, name) describe what the
field *is* rather than merely that something was supplied, and `ai_server/llm/
validation.py` routes every model-proposed write through these same functions. They are
the single authority: there is no second copy of an address rule anywhere. Only the
identity fields (2-5) are affected, so the deliberate lockstep with
`AssessmentDraftService.php` on the draft fields (6-9) is untouched.
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
# A date of birth older than this is not a person's date of birth, it is a bad parse or
# a bad guess (TICK-061: "a date like a plausible date").
_MAX_PLAUSIBLE_AGE_YEARS = 120
_MAX_TEXT_FIELD_LENGTH = 100
_MAX_ACCOMMODATION_DETAIL_LENGTH = 200
_PHONE_PATTERN = re.compile(r"^\+1\d{10}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZIP_PATTERN = re.compile(r"^\d{5}(-?\d{4})?$")

# --- Shape rules for the free-text fields (TICK-061) --------------------------------
#
# TICK-050 shipped a street rule that accepted any non-empty string, so
# `"Update it to: 2002 Bridge Avenue"` -- a lead-in phrase the freeform parser had
# handed over unexamined -- was written into a patient's chart. A validator that
# accepts whatever it is given is a mirror, not a safety net. These rules describe what
# each field *is*, so they hold whatever produced the value: a regex parser, a language
# model, or a future one of either.
#
# Refusing a value a patient could plausibly hold is the accepted cost (PRD NFR-36: a
# refusal is an acceptable outcome, a wrong write is not), so the rules stay structural
# and the refusal always says what to send instead.

_STREET_PUNCTUATION = " .,'#&/-"
_UNIT_PUNCTUATION = " .,'#&/-"
_CITY_PUNCTUATION = " .'-"
_NAME_PUNCTUATION = " .'-"

_MAX_STREET_WORDS = 10
_MAX_UNIT_WORDS = 6
_MAX_CITY_WORDS = 5
_MAX_NAME_WORDS = 5

# A street line begins with the thing that makes it a street line: a house number
# (`2002`, `42A`, `120-14`) or a post-office box. "Update it to: 2002 Bridge Avenue"
# fails here even with its punctuation removed, and so does an answer to some other
# question that happens to contain an address.
_HOUSE_NUMBER_PATTERN = re.compile(
    r"^(?:\d{1,6}[A-Za-z]?(?:\s?-\s?\d{1,6}[A-Za-z]?)?|p\.?\s*o\.?\s+box|post\s+office\s+box)\b",
    re.IGNORECASE,
)

# Words that belong to a conversation, not to an address or a name. This catches the
# rest of the observed class -- a trailing question ("2002 Bridge Avenue is that
# right"), or an answer to a different question -- once punctuation alone no longer
# gives it away. Deliberately excludes prepositions and articles, which do appear in
# real place names ("Avenue of the Americas"), and is applied only to multi-word values,
# so a one-word legitimate name ("My", "Me") is never refused for looking like a pronoun.
_CONVERSATIONAL_WORDS: frozenset[str] = frozenset(
    {
        "actually",
        "address",
        "am",
        "appointment",
        "are",
        "ask",
        "asked",
        "be",
        "been",
        "being",
        "birthday",
        "can",
        "cannot",
        "change",
        "changed",
        "changing",
        "correct",
        "corrected",
        "could",
        "did",
        "do",
        "does",
        "email",
        "fix",
        "fixed",
        "had",
        "has",
        "have",
        "how",
        "instead",
        "is",
        "it",
        "its",
        "maybe",
        "me",
        "mine",
        "move",
        "moved",
        "moving",
        "my",
        "name",
        "need",
        "okay",
        "phone",
        "please",
        "said",
        "says",
        "should",
        "sorry",
        "tell",
        "thank",
        "thanks",
        "think",
        "update",
        "updated",
        "updating",
        "want",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "would",
        "yes",
        "you",
        "your",
        "yours",
    }
)


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


def _collapsed_text(value: object, *, label: str, max_length: int) -> str:
    """Return `value` as a single-spaced string, or raise if it is not usable text."""
    if not isinstance(value, str):
        raise FieldValidationError([f"{label} must be text"])
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > max_length:
        raise FieldValidationError([f"{label} must be 1-{max_length} non-whitespace characters"])
    return collapsed


def _only(text: str, punctuation: str, *, digits: bool) -> bool:
    """Report whether `text` is built solely from letters, `punctuation`, and digits."""
    return all(
        character.isalpha() or (digits and character.isdigit()) or character in punctuation
        for character in text
    )


def _reads_as_conversation(words: list[str]) -> bool:
    """Report whether a multi-word value contains a word that belongs to a sentence.

    Single-word values are exempt: they cannot be a lead-in phrase or a question, and a
    real given name or town can legitimately be spelled like an English pronoun.
    """
    if len(words) < 2:
        return False
    return any(word.strip(_STREET_PUNCTUATION).lower() in _CONVERSATIONAL_WORDS for word in words)


def validate_street(value: object, *, label: str = "street1") -> str:
    """Validate that `value` is a street line and not a sentence containing one.

    Requires a house number or PO box, at least one following word, only the characters
    a street line is written with, and no conversational word. This is the rule that
    would have refused `"Update it to: 2002 Bridge Avenue"` (TICK-050); it is applied
    the same way whether a parser, a model, or a form produced the value.
    """
    example = f"{label} must be a street line such as '100 Maple Ave'"
    collapsed = _collapsed_text(value, label=label, max_length=_MAX_TEXT_FIELD_LENGTH)
    words = collapsed.split(" ")
    if (
        not _only(collapsed, _STREET_PUNCTUATION, digits=True)
        or len(words) < 2
        or len(words) > _MAX_STREET_WORDS
        or not _HOUSE_NUMBER_PATTERN.match(collapsed)
        or _reads_as_conversation(words)
    ):
        raise FieldValidationError([example])
    return collapsed


def validate_street_unit(value: object, *, label: str = "street2") -> str:
    """Validate an apartment, suite, or unit line -- no house number required."""
    example = f"{label} must be a unit line such as 'Apt 4B'"
    collapsed = _collapsed_text(value, label=label, max_length=_MAX_TEXT_FIELD_LENGTH)
    words = collapsed.split(" ")
    if (
        not _only(collapsed, _UNIT_PUNCTUATION, digits=True)
        or len(words) > _MAX_UNIT_WORDS
        or _reads_as_conversation(words)
    ):
        raise FieldValidationError([example])
    return collapsed


def validate_city(value: object, *, label: str = "city") -> str:
    """Validate a town or city name: letters and place-name punctuation only."""
    example = f"{label} must be a town or city name such as 'Springfield'"
    collapsed = _collapsed_text(value, label=label, max_length=_MAX_TEXT_FIELD_LENGTH)
    words = collapsed.split(" ")
    if (
        not _only(collapsed, _CITY_PUNCTUATION, digits=False)
        or len(words) > _MAX_CITY_WORDS
        or not collapsed[0].isalpha()
        or _reads_as_conversation(words)
    ):
        raise FieldValidationError([example])
    return collapsed


def validate_text_name(value: object, *, label: str) -> str:
    """Validate a legal given/family name: 1-100 non-whitespace characters, and a name.

    TICK-061 added the second half: a name is letters and name punctuation, at most a
    few words, and never a sentence -- so `"My name is Avery"` is refused rather than
    written into the record as a given name.
    """
    collapsed = _collapsed_text(value, label=label, max_length=_MAX_TEXT_FIELD_LENGTH)
    words = collapsed.split(" ")
    if (
        not _only(collapsed, _NAME_PUNCTUATION, digits=False)
        or len(words) > _MAX_NAME_WORDS
        or not collapsed[0].isalpha()
        or _reads_as_conversation(words)
    ):
        raise FieldValidationError([f"{label} must be a person's name such as 'Avery'"])
    return collapsed


def validate_date_of_birth(value: object, *, now: datetime) -> str:
    """Validate a calendar date that is not in the future and is a plausible adult's."""
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
    if age > _MAX_PLAUSIBLE_AGE_YEARS:
        raise FieldValidationError(
            [f"date_of_birth is more than {_MAX_PLAUSIBLE_AGE_YEARS} years ago"]
        )
    return parsed.isoformat()


def validate_address(value: object) -> Address:
    """Validate street line 1, city, two-letter state/territory, and ZIP."""
    if not isinstance(value, dict):
        raise FieldValidationError(["address must be an object"])
    errors: list[str] = []

    try:
        street1 = validate_street(value.get("street1"))
    except FieldValidationError as exc:
        errors.extend(exc.details)
        street1 = ""

    # A second line is optional, and a whitespace-only one means the patient gave none;
    # anything else present must still read as a unit line.
    raw_street2 = value.get("street2")
    street2: str | None = None
    if raw_street2 is not None and (not isinstance(raw_street2, str) or raw_street2.strip()):
        try:
            street2 = validate_street_unit(raw_street2)
        except FieldValidationError as exc:
            errors.extend(exc.details)

    try:
        city = validate_city(value.get("city"))
    except FieldValidationError as exc:
        errors.extend(exc.details)
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
        street1=street1,
        city=city,
        state=state.upper(),
        zip_code=zip_code,
        street2=street2,
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
