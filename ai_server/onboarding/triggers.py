"""Deterministic local supportive-content triggers (`ONBOARDING_CONTRACT.md`,
"Supportive-content rules" and "Local distress phrase corpus").

Nothing here calls an external model, a sentiment score, or a clinical classifier --
a fixed substring match against a small, approved phrase list selects one of four
fixed, approved strings, or nothing. This is the mechanism the outbound privacy
policy relies on to keep any patient message local (`ai_server/privacy/gate.py`
governs the separate, unrelated question of what may leave the process at all; this
module never sends anything anywhere).
"""

from __future__ import annotations

import re
from enum import Enum

from ai_server.ocr.service import ExtractedIdentity

LONG_PAUSE_THRESHOLD_SECONDS = 120.0


class Trigger(str, Enum):
    """The only four supportive-content triggers the approved contract defines."""

    LONG_PAUSE = "long_pause"
    UPLOAD_FAILURE = "upload_failure"
    GENERAL_DISTRESS = "general_distress"
    IMMEDIATE_SAFETY = "immediate_safety"


SUPPORTIVE_CONTENT: dict[Trigger, str] = {
    Trigger.LONG_PAUSE: (
        "Take your time. Your progress is saved, and you can continue when you’re ready."
    ),
    Trigger.UPLOAD_FAILURE: (
        "That image didn’t work, but you can continue by entering your details manually. "
        "We won’t guess any missing information."
    ),
    Trigger.GENERAL_DISTRESS: ("I’m sorry this feels difficult. You can pause or continue later."),
    Trigger.IMMEDIATE_SAFETY: (
        "If you might hurt yourself or are in immediate danger, call or text 988 in the "
        "U.S., call 911, or contact local emergency services."
    ),
}

# ONBOARDING_CONTRACT.md "Local distress phrase corpus": lowercase, whitespace-
# collapsed substring matches only. The immediate-safety list takes precedence over
# the general-distress list. Expand only through an approved contract change and
# matching fixtures -- never inferred, scored, or derived.
GENERAL_DISTRESS_PHRASES: tuple[str, ...] = (
    "i feel overwhelmed",
    "i can't do this",
    "this is too much",
    "i am panicking",
    "i feel anxious",
    "i am scared",
    "i feel stressed",
    "i need emotional help",
)
IMMEDIATE_SAFETY_PHRASES: tuple[str, ...] = (
    "i am suicidal",
    "i am thinking about suicide",
    "i want to kill myself",
    "i want to die",
    "i want to end my life",
    "i want to hurt myself",
    "i am self harming",
    "i am self-harming",
    "i can't keep myself safe",
    "i am in immediate danger",
)

_WHITESPACE = re.compile(r"\s+")

# Curly/smart-quote apostrophe variants a phone keyboard or word processor's
# autocorrect commonly substitutes for a straight one -- every phrase in the corpus
# below is written with a straight apostrophe, so these must collapse to it or a
# message like "I can’t keep myself safe" would silently fail to match.
_SMART_APOSTROPHES = re.compile("[‘’ʼ]")


def _normalize(message: str) -> str:
    unquoted = _SMART_APOSTROPHES.sub("'", message)
    return _WHITESPACE.sub(" ", unquoted.strip().lower())


def detect_distress(message: str) -> Trigger | None:
    """Return the one matching distress trigger for `message`, or `None`.

    Immediate-safety phrases are checked first so a message matching both lists
    (none currently do, but the precedence is part of the approved contract) always
    resolves to the more urgent content.
    """
    normalized = _normalize(message)
    if any(phrase in normalized for phrase in IMMEDIATE_SAFETY_PHRASES):
        return Trigger.IMMEDIATE_SAFETY
    if any(phrase in normalized for phrase in GENERAL_DISTRESS_PHRASES):
        return Trigger.GENERAL_DISTRESS
    return None


def upload_failure_trigger(identity: ExtractedIdentity | None) -> Trigger | None:
    """Return `UPLOAD_FAILURE` for a failed upload/extraction, `None` otherwise.

    `identity=None` covers local client/server validation rejecting the upload
    before OCR ever ran (`ai_server/ocr/service.py`'s `InvalidUploadError` family).
    A wholly empty `ExtractedIdentity` covers OCR running but extracting nothing. A
    partial or complete result is success, per the contract's "Successful or partial
    uploads proceed to the confirmation fields" -- neither shows this content.
    """
    if identity is None:
        return Trigger.UPLOAD_FAILURE
    if identity.name is None and identity.date_of_birth is None and identity.address is None:
        return Trigger.UPLOAD_FAILURE
    return None


class PauseTracker:
    """Tracks whether the once-per-field long-pause message has already been shown.

    One instance covers one in-progress flow (request-duration only, per
    ARCHITECTURE.md Sec. 5 -- nothing here is persisted).
    """

    def __init__(self) -> None:
        self._shown_fields: set[str] = set()

    def check(self, field: str, idle_seconds: float) -> Trigger | None:
        """Return `LONG_PAUSE` once per `field` once `idle_seconds` crosses the threshold."""
        if idle_seconds < LONG_PAUSE_THRESHOLD_SECONDS or field in self._shown_fields:
            return None
        self._shown_fields.add(field)
        return Trigger.LONG_PAUSE
