"""Routes a chat turn into `OnboardingFlow` once a session is mid-onboarding, or an
explicit request starts one (TICK-035).

`OnboardingFlow`'s own docstring is explicit that field capture, validation, and
checkpointing are deterministic local operations that never call an external model --
this module is that separate turn-handling path, never `GroqWorkflow`'s (`chat.py`).
A field answer here is a small JSON value shaped for that field (matching the guided,
one-field-at-a-time nature of `ONBOARDING_CONTRACT.md`, not a freeform conversational
parse); `fields.validate_field`'s own messages guide a patient who sends the wrong
shape back to the right one, and nothing here ever forwards a field value to Groq.

Restart-safety note: only `OnboardingCursor` (the draft id) and the OpenEMR draft's own
checkpointed fields 6-9 survive an AI-server restart, per ARCHITECTURE.md Sec. 5 ("a
non-patient workflow cursor in SQLite"); identity fields 2-5 are held only in this
process's memory until `complete()`, exactly like `PauseTracker`
(`ai_server/onboarding/triggers.py`, "request-duration only ... nothing here is
persisted"). This module therefore collects the OpenEMR draft fields (6-9) first, so a
restart mid-flow always resumes from OpenEMR (FR-30), and the four identity fields
last, immediately before completion, to minimize what a restart can lose.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable

from ai_server.app.auth import SessionStore, utc_now
from ai_server.onboarding.draft_client import AssessmentDraftNotFoundError
from ai_server.onboarding.fields import IDENTITY_FIELDS, FieldValidationError, validate_field
from ai_server.onboarding.flow import (
    FieldCheckpointRejected,
    OnboardingCursor,
    OnboardingFlow,
    OnboardingIncompleteError,
)
from ai_server.onboarding.triggers import SUPPORTIVE_CONTENT, PauseTracker, detect_distress
from ai_server.openemr.adapter import OpenEmrRequestError

UNAVAILABLE_ONBOARDING_RESPONSE = (
    "The guided onboarding assistant is unavailable right now. Please try again "
    "shortly, or ask a member of staff to help you register in person."
)

_DRAFT_FIELD_ORDER: tuple[str, ...] = (
    "preferred_contact",
    "help_type",
    "visit_preference",
    "accommodations",
)
_IDENTITY_FIELD_ORDER: tuple[str, ...] = (
    "given_name",
    "family_name",
    "date_of_birth",
    "address",
)
_DRAFT_PRESENCE_KEYS: dict[str, tuple[str, ...]] = {
    "preferred_contact": ("preferred_contact_method",),
    "help_type": ("help_type",),
    "visit_preference": ("visit_format", "visit_time_window"),
    "accommodations": ("accommodations",),
}

FIELD_PROMPTS: dict[str, str] = {
    "preferred_contact": (
        "How should we contact you? Reply as JSON, e.g. "
        '{"method": "phone", "value": "+15551234567"}, '
        '{"method": "email", "value": "you@example.com"}, or {"method": "portal_message"}.'
    ),
    "help_type": (
        "What would you like help with? Reply with one of: counseling_or_therapy, "
        "psychiatric_evaluation_or_medication_support, both, not_sure_yet."
    ),
    "visit_preference": (
        "What visit format and time window work best? Reply as JSON, e.g. "
        '{"format": "video", "time_window": "weekday_morning"}.'
    ),
    "accommodations": (
        "Do you need any language or accessibility accommodations? Reply as JSON, e.g. "
        '{"selected": ["language_interpreter"], "detail": "(optional, if other)"}, or '
        '{"selected": []} for none.'
    ),
    "given_name": "What is your legal given (first) name?",
    "family_name": "What is your legal family (last) name?",
    "date_of_birth": "What is your date of birth? Use YYYY-MM-DD.",
    "address": (
        "What is your mailing address? Reply as JSON, e.g. "
        '{"street1": "100 Maple Ave", "city": "Springfield", "state": "IL", '
        '"zip_code": "62704", "street2": "(optional)"}.'
    ),
}

_START_PHRASES: tuple[str, ...] = (
    "start onboarding",
    "begin onboarding",
    "start my onboarding",
    "start the onboarding",
    "start intake",
    "begin intake",
    "start my assessment",
    "begin my assessment",
    "start my registration",
    "complete my onboarding",
    "complete onboarding",
    "get started with onboarding",
)
_CONFIRM_PHRASES: tuple[str, ...] = ("confirm", "confirm and complete", "yes complete", "i confirm")


def _normalize(message: str) -> str:
    return " ".join(message.strip().lower().split())


def _parsed_action(message: str) -> str | None:
    try:
        parsed = json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("action"), str):
        return parsed["action"]
    return None


def is_onboarding_start_request(message: str) -> bool:
    """True for a deterministic, local-only signal that a patient wants to begin
    guided onboarding: either a machine-sent `{"action": "start_onboarding"}` or one
    of a small fixed corpus of phrases, in the same "fixed local phrase list" spirit
    as `triggers.py`'s distress detection -- never an LLM classification."""
    if _parsed_action(message) == "start_onboarding":
        return True
    normalized = _normalize(message)
    return any(phrase in normalized for phrase in _START_PHRASES)


def is_confirmation(message: str) -> bool:
    """True for the explicit review-screen confirmation `ONBOARDING_CONTRACT.md`
    requires before completion is attempted."""
    if _parsed_action(message) == "confirm":
        return True
    return _normalize(message) in _CONFIRM_PHRASES


def onboarding_mode(cursor: str | None, message: str) -> bool:
    """True when this turn belongs to `OnboardingFlow`, not the scheduling
    `GroqWorkflow`.

    A present cursor means a draft is already in progress, and every further turn
    stays in onboarding regardless of content (AC3, TICK-035); with no cursor, only an
    explicit request to start switches modes (AC2) -- everything else keeps using the
    existing scheduling workflow, unchanged (no regression to TICK-034).
    """
    return cursor is not None or is_onboarding_start_request(message)


def _parse_value(message: str) -> object:
    """Best-effort JSON parse; a plain-text answer (name, date, `help_type` choice)
    is valid input to `validate_field` exactly as typed, so a parse failure falls back
    to the raw message rather than rejecting it outright."""
    try:
        return json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return message


def _next_field(identity: dict[str, object], draft_fields: dict[str, object]) -> str | None:
    """The next field this flow still needs, or `None` once all eight are answered.

    Presence (`key in draft_fields`), not truthiness, decides a draft field is done --
    `accommodations` is optional and a deliberate empty selection (`[]`) is a valid,
    complete answer that must not be re-asked.
    """
    for name in _DRAFT_FIELD_ORDER:
        keys = _DRAFT_PRESENCE_KEYS[name]
        if not all(key in draft_fields for key in keys):
            return name
    for name in _IDENTITY_FIELD_ORDER:
        if name not in identity:
            return name
    return None


def _rejection_text(details: list[str]) -> str:
    return "That value could not be accepted: " + "; ".join(details)


def _review_summary(identity: dict[str, object], draft_fields: dict[str, object]) -> str:
    parts = [
        f"{name}={draft_fields.get(_DRAFT_PRESENCE_KEYS[name][0])!r}" for name in _DRAFT_FIELD_ORDER
    ]
    parts += [f"{name}={identity.get(name)!r}" for name in _IDENTITY_FIELD_ORDER]
    return (
        "Review your answers before completing onboarding: "
        + ", ".join(parts)
        + ". Reply CONFIRM to finish."
    )


@dataclass
class _SessionState:
    """In-process-only state for one session's in-progress flow (never in SQLite,
    same "request-duration" discipline `PauseTracker` already documents)."""

    identity: dict[str, object] = field(default_factory=dict)
    pause_tracker: PauseTracker = field(default_factory=PauseTracker)
    last_turn_at: datetime | None = None
    awaiting_confirmation: bool = False


@dataclass
class OnboardingChatService:
    """Streams one onboarding turn through `OnboardingFlow`, never through Groq."""

    flow: OnboardingFlow | None
    session_store: SessionStore
    clock: Callable[[], datetime] = utc_now
    _sessions: dict[str, _SessionState] = field(default_factory=dict)

    async def stream_reply(self, handle: str, message: str) -> AsyncIterator[str]:
        """Yield the fixed unavailable message, or the next onboarding step's reply."""
        if self.flow is None:
            yield UNAVAILABLE_ONBOARDING_RESPONSE
            return
        now = self.clock()
        access_token = await asyncio.to_thread(self.session_store.access_token, handle, now)
        if access_token is None:
            yield UNAVAILABLE_ONBOARDING_RESPONSE
            return

        distress = detect_distress(message)
        if distress is not None:
            yield SUPPORTIVE_CONTENT[distress]
            return

        state = self._sessions.setdefault(handle, _SessionState())
        idle_seconds = (now - state.last_turn_at).total_seconds() if state.last_turn_at else 0.0
        state.last_turn_at = now

        cursor_raw = await asyncio.to_thread(self.session_store.load_cursor, handle, now)
        started_this_turn = cursor_raw is None
        if started_this_turn:
            cursor = await self.flow.start(access_token)
            await asyncio.to_thread(self.session_store.save_cursor, handle, cursor.serialize(), now)
            draft_fields: dict[str, object] = {}
        else:
            cursor = OnboardingCursor.deserialize(cursor_raw)
            try:
                draft = await self.flow.resume(access_token, cursor)
            except AssessmentDraftNotFoundError:
                yield (
                    "Your onboarding draft could not be found; it may have expired. "
                    "Please start onboarding again."
                )
                return
            draft_fields = draft.fields

        next_field = _next_field(state.identity, draft_fields)

        if started_this_turn:
            assert next_field is not None  # four required draft fields always remain
            yield self._pause_prefix(state, next_field, idle_seconds) + FIELD_PROMPTS[next_field]
            return

        if state.awaiting_confirmation:
            async for chunk in self._handle_confirmation(
                handle, access_token, cursor, state, message, now
            ):
                yield chunk
            return

        if next_field is None:
            state.awaiting_confirmation = True
            yield _review_summary(state.identity, draft_fields)
            return

        pause_text = self._pause_prefix(state, next_field, idle_seconds)
        value = _parse_value(message)
        if next_field in IDENTITY_FIELDS:
            try:
                validate_field(next_field, value, now=now)
            except FieldValidationError as exc:
                yield pause_text + _rejection_text(exc.details)
                return
            # Store the raw value, not the validated result: `OnboardingFlow.complete`
            # re-validates every identity field itself from exactly this raw shape
            # (e.g. `address` as a plain dict, not the `Address` this call returns).
            state.identity[next_field] = value
            following = _next_field(state.identity, draft_fields)
        else:
            try:
                await self.flow.checkpoint_field(access_token, cursor, next_field, value, now)
            except FieldCheckpointRejected as exc:
                yield pause_text + _rejection_text(exc.details)
                return
            updated_draft = await self.flow.resume(access_token, cursor)
            draft_fields = updated_draft.fields
            following = _next_field(state.identity, draft_fields)

        if following is None:
            state.awaiting_confirmation = True
            yield pause_text + _review_summary(state.identity, draft_fields)
            return
        yield pause_text + FIELD_PROMPTS[following]

    async def _handle_confirmation(
        self,
        handle: str,
        access_token: str,
        cursor: OnboardingCursor,
        state: _SessionState,
        message: str,
        now: datetime,
    ) -> AsyncIterator[str]:
        assert self.flow is not None
        if not is_confirmation(message):
            draft = await self.flow.resume(access_token, cursor)
            yield _review_summary(state.identity, draft.fields)
            return
        try:
            record = await self.flow.complete(access_token, cursor, state.identity, now)
        except OnboardingIncompleteError as exc:
            yield _rejection_text(exc.details)
            return
        except OpenEmrRequestError:
            yield (
                "We couldn't finish saving your onboarding just now; your progress "
                "is saved. Please try confirming again."
            )
            return
        self._sessions.pop(handle, None)
        await asyncio.to_thread(self.session_store.save_cursor, handle, "", now)
        yield (
            f"Thanks, {record.given_name}! Your onboarding is complete and saved to "
            "your OpenEMR record."
        )

    @staticmethod
    def _pause_prefix(state: _SessionState, field_name: str, idle_seconds: float) -> str:
        trigger = state.pause_tracker.check(field_name, idle_seconds)
        if trigger is None:
            return ""
        return SUPPORTIVE_CONTENT[trigger] + " "


def unavailable_onboarding_service(
    session_store: SessionStore, clock: Callable[[], datetime] = utc_now
) -> OnboardingChatService:
    """Return a service that always reports the fixed unavailable message."""
    return OnboardingChatService(flow=None, session_store=session_store, clock=clock)
