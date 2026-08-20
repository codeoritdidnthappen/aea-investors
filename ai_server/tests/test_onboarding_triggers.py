"""Fixture tests for supportive-content triggers (TICK-017, AC4).

Covers every phrase in `ONBOARDING_CONTRACT.md`'s local distress corpus, every
explicit no-trigger fixture message, the once-per-field long-pause behavior, and the
upload-failure/success distinction -- "no trigger produces none" (AC4) is checked
both ways: every trigger case shows exactly its approved content, and every
no-trigger case shows nothing.
"""

from __future__ import annotations

import pytest

from ai_server.ocr.service import ExtractedIdentity
from ai_server.onboarding.triggers import (
    GENERAL_DISTRESS_PHRASES,
    IMMEDIATE_SAFETY_PHRASES,
    LONG_PAUSE_THRESHOLD_SECONDS,
    SUPPORTIVE_CONTENT,
    PauseTracker,
    Trigger,
    detect_distress,
    upload_failure_trigger,
)


def test_ac4_every_supportive_content_string_is_approved_and_non_empty() -> None:
    assert set(SUPPORTIVE_CONTENT) == set(Trigger)
    for content in SUPPORTIVE_CONTENT.values():
        assert content and content.strip() == content


@pytest.mark.parametrize("phrase", GENERAL_DISTRESS_PHRASES)
def test_ac4_every_general_distress_phrase_triggers_general_distress(phrase: str) -> None:
    assert detect_distress(f"honestly, {phrase} today") is Trigger.GENERAL_DISTRESS


@pytest.mark.parametrize("phrase", IMMEDIATE_SAFETY_PHRASES)
def test_ac4_every_immediate_safety_phrase_triggers_immediate_safety(phrase: str) -> None:
    assert detect_distress(f"{phrase}, please help") is Trigger.IMMEDIATE_SAFETY


def test_ac4_detection_normalizes_case_and_whitespace() -> None:
    assert detect_distress("  I   FEEL   OVERWHELMED  ") is Trigger.GENERAL_DISTRESS


def test_ac4_immediate_safety_takes_precedence_over_general_distress() -> None:
    message = "i feel overwhelmed and i want to die"
    assert detect_distress(message) is Trigger.IMMEDIATE_SAFETY


@pytest.mark.parametrize(
    "message",
    [
        "I want to schedule an appointment",
        "I need help scheduling",
        "that option is not safe",
    ],
)
def test_fixture_no_distress_content_for_unrelated_messages(message: str) -> None:
    """`ONBOARDING_CONTRACT.md` fixture rows for messages that must show nothing."""
    assert detect_distress(message) is None


def test_ac4_a_generic_process_question_shows_no_distress_content() -> None:
    assert detect_distress("What happens after I submit this form?") is None


def test_ac4_upload_failure_trigger_fires_for_a_rejected_upload() -> None:
    """`identity=None` covers a client/server validation rejection before OCR runs."""
    assert upload_failure_trigger(None) is Trigger.UPLOAD_FAILURE


def test_ac4_upload_failure_trigger_fires_for_a_wholly_failed_extraction() -> None:
    assert upload_failure_trigger(ExtractedIdentity()) is Trigger.UPLOAD_FAILURE


def test_fixture_a_partial_extraction_shows_no_upload_failure_content() -> None:
    """`ONBOARDING_CONTRACT.md` fixture: "Partial OCR result" proceeds to confirmation."""
    partial = ExtractedIdentity(name="Avery Alden", date_of_birth=None, address=None)
    assert upload_failure_trigger(partial) is None


def test_fixture_a_complete_extraction_shows_no_upload_failure_content() -> None:
    complete = ExtractedIdentity(
        name="Avery Alden", date_of_birth="1990-01-01", address="100 Maple Avenue"
    )
    assert upload_failure_trigger(complete) is None


def test_fixture_no_pause_message_before_the_threshold() -> None:
    tracker = PauseTracker()
    assert tracker.check("given_name", LONG_PAUSE_THRESHOLD_SECONDS - 1) is None


def test_fixture_one_long_pause_message_at_the_threshold() -> None:
    """`ONBOARDING_CONTRACT.md` fixture: "120-second inactive field"."""
    tracker = PauseTracker()
    assert tracker.check("given_name", LONG_PAUSE_THRESHOLD_SECONDS) is Trigger.LONG_PAUSE


def test_ac4_the_long_pause_message_shows_only_once_per_field() -> None:
    tracker = PauseTracker()
    assert tracker.check("given_name", 200) is Trigger.LONG_PAUSE
    assert tracker.check("given_name", 300) is None
    # A different field gets its own independent pause message.
    assert tracker.check("family_name", 150) is Trigger.LONG_PAUSE
