"""Routes a chat turn into a local, confirm-then-write address update (TICK-050).

This is a deterministic local path that never calls Groq, routed *before* the Groq
chat service in `POST /api/chat` exactly as guided onboarding already is
(`ai_server/app/onboarding_chat.py`, TICK-035) -- not an `AuthoritativeTool` and not a
new `PlanningOutput` intent. Two independent reasons make the planner the wrong home
for this:

- `PlanningOutput` (`ai_server/llm/groq.py`) is a closed `Literal` of four scheduling
  intents under `extra="forbid"`, injected as a Groq *strict* JSON schema, so it is
  structurally incapable of expressing an address update.
- More importantly, the address must never reach Groq at all (NFR-2). The Presidio
  gate (`ai_server/privacy/gate.py`) flags an address and returns `LOCAL_CORRECTION`
  before planning even runs, so routing one through the planner would either be
  blocked there or, if it slipped through, violate NFR-2 outright.

Nothing in this module sends anything anywhere except the one OpenEMR write the
patient explicitly confirms, through TICK-049's address-only structured path
(`OpenEmrDemographicsAdapter.write_confirmed_address`), which names only the address
columns so name and date of birth are left untouched.

Freeform entry, not JSON: onboarding's address prompt asks a patient to type a raw
JSON object, which is defensible for a guided, one-field-at-a-time intake form but not
for a patient who simply says "I moved" in general chat -- and not against NFR-19's
plain-language bar. `parse_address_reply` therefore parses an ordinary comma-separated
address locally (still accepting onboarding's JSON shape, so a machine-sent client can
use it), and hands the result to the *same* `fields.validate_address` onboarding uses,
so there is exactly one set of address rules. A parse that comes up short produces a
partial mapping on purpose: `validate_address` then names the specific missing or
malformed component rather than this module inventing its own message.

State note: the general chat is stateless per turn, so this flow keeps its own
short-lived, in-process session state (`_AddressSessionState`), like onboarding's
identity fields and `PauseTracker`. It is deliberately never persisted -- a pending,
unconfirmed address is a patient value, and `sessions.cursor` is reserved for the
onboarding draft id and nothing else (`ai_server/app/auth.py`, `save_cursor`). An
AI-server restart mid-flow therefore loses only an unconfirmed draft, never a write,
which the ticket's Out of Scope explicitly accepts.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Callable

from ai_server.app.auth import SessionStore, utc_now
from ai_server.onboarding.fields import Address, FieldValidationError, validate_address
from ai_server.onboarding.triggers import SUPPORTIVE_CONTENT, detect_distress
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.openemr.demographics import (
    ConfirmedAddress,
    IdentityNotConfirmedError,
    OpenEmrDemographicsAdapter,
    confirm_address,
)

UNAVAILABLE_ADDRESS_RESPONSE = (
    "Updating your address in this chat is unavailable right now, so nothing has been "
    "changed. Please try again shortly, or ask a member of staff to update your address "
    "for you."
)

_EXAMPLE_ADDRESS = "100 Maple Ave, Apt 4B, Springfield, IL 62704"

ADDRESS_PROMPT = (
    "I can update the mailing address on your record. What is your new address? "
    "Please send the street, city, state, and ZIP code together, for example: "
    f"{_EXAMPLE_ADDRESS}. Include an apartment or unit line if you have one. "
    "Nothing is saved until you have checked it and confirmed."
)

CANCELLED_RESPONSE = (
    "No problem -- I've stopped the address update and nothing was saved, so your "
    "record is unchanged. What else can I help you with?"
)

# TICK-041's rule: a failed write is reported as a failure and never described as
# success. The pending address is deliberately kept so the patient can just confirm
# again rather than retyping it (mirrors `_handle_confirmation` in onboarding_chat.py).
WRITE_FAILED_RESPONSE = (
    "I couldn't save your address just now, so nothing was written and your record is "
    "unchanged. Reply CONFIRM to try again, or CANCEL to stop."
)

# Deliberately does not itself say "nothing has been saved" -- `_review_summary` already
# does, and saying it twice in one reply reads as a glitch (caught in the live transcript
# in `evidence/TICK-050/`).
_NOT_CONFIRMED_PREFIX = "I didn't recognise that as a confirmation. "

# Substring-matched, like onboarding's start corpus: every phrase here names the
# address itself, so it cannot collide with a scheduling request ("change my
# appointment") or with onboarding's own start phrases, none of which mention an
# address.
_ADDRESS_UPDATE_PHRASES: tuple[str, ...] = (
    "update my address",
    "update my mailing address",
    "update my home address",
    "change my address",
    "change my mailing address",
    "change my home address",
    "correct my address",
    "fix my address",
    "my address is wrong",
    "my address has changed",
    "update the address on my record",
    "update my address on file",
)
# Exact-matched, not substring-matched: these do not name an address, so "I moved my
# appointment to Friday" must stay a scheduling turn. Requiring the whole message keeps
# them unambiguous.
_MOVED_PHRASES: tuple[str, ...] = (
    "i moved",
    "i've moved",
    "i have moved",
    "i just moved",
    "i moved recently",
    "i moved house",
    "we moved",
)
_CONFIRM_PHRASES: tuple[str, ...] = (
    "confirm",
    "yes",
    "yes confirm",
    "yes save it",
    "save it",
    "i confirm",
)
_CANCEL_PHRASES: tuple[str, ...] = (
    "cancel",
    "never mind",
    "nevermind",
    "stop",
    "forget it",
    "no thanks",
    "no thank you",
    "quit",
    "exit",
)

# A whitespace-free trailing part that starts with a digit is a bare ZIP ("..., IL,
# 62704"); "..., Springfield, Illinois" is not, and must not silently promote "Illinois"
# to the ZIP and the city to the state.
_BARE_ZIP = re.compile(r"^\d[\d-]*$")


def _normalize(message: str) -> str:
    """Lowercase, whitespace-collapse, and drop trailing sentence punctuation.

    Onboarding's own `_normalize` keeps punctuation, which is why `is_confirmation`
    there rejects "Confirm."; the phrases matched here are whole short replies typed by
    a patient ("Yes.", "I moved."), so a trailing period must not change the meaning.
    The curly apostrophe every mobile keyboard autocorrects to is folded to a straight
    one so "I've moved" matches whichever way it was typed.
    """
    folded = message.strip().lower().replace("’", "'")
    return " ".join(folded.split()).rstrip(".!?")


def _parsed_action(message: str) -> str | None:
    try:
        parsed = json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("action"), str):
        return parsed["action"]
    return None


def is_address_update_request(message: str) -> bool:
    """True for a deterministic, local-only signal that a patient wants to update their
    address: either a machine-sent `{"action": "update_address"}` or one of a small
    fixed phrase corpus, in the same "fixed local phrase list" spirit as
    `is_onboarding_start_request` and `triggers.py`'s distress detection -- never an
    LLM classification (the message could not be sent to one anyway, NFR-2)."""
    if _parsed_action(message) == "update_address":
        return True
    normalized = _normalize(message)
    if any(phrase in normalized for phrase in _ADDRESS_UPDATE_PHRASES):
        return True
    return normalized in _MOVED_PHRASES


def is_confirmation(message: str) -> bool:
    """True only for an explicit confirmation of the reviewed address.

    Everything else -- including silence-adjacent replies like "ok" or "sure" -- is not
    a confirmation, so `_handle_confirmation` re-shows the review and writes nothing
    (TICK-016's confirmed-only rule, FR-26).
    """
    if _parsed_action(message) == "confirm_address_update":
        return True
    return _normalize(message) in _CONFIRM_PHRASES


def is_cancellation(message: str) -> bool:
    """True for an explicit request to abandon the flow and return to normal chat."""
    if _parsed_action(message) == "cancel_address_update":
        return True
    return _normalize(message) in _CANCEL_PHRASES


def address_update_mode(cursor: str | None, update_in_progress: bool, message: str) -> bool:
    """True when this turn belongs to the local address-update flow.

    Guided onboarding always wins: a present cursor means a draft is in progress and
    every further turn stays in onboarding regardless of content (TICK-035 AC3), so a
    patient mid-onboarding is never hijacked into this flow -- their address is already
    being collected there. Otherwise an in-progress update keeps its own turns, and with
    neither, only an explicit request switches modes; everything else keeps using the
    existing scheduling workflow, unchanged.
    """
    if cursor is not None:
        return False
    return update_in_progress or is_address_update_request(message)


def parse_address_reply(message: str) -> object:
    """Parse a patient's address reply locally into a mapping for `validate_address`.

    Accepts onboarding's JSON object shape unchanged, so a machine-sent client can keep
    using it, and otherwise parses an ordinary comma-separated address. Returns whatever
    it could recover -- possibly partial, possibly nothing -- and never raises:
    `validate_address` is the single authority on whether an address is acceptable, and
    naming the specific missing component is its job, not this function's.
    """
    try:
        parsed = json.loads(message)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return _parse_freeform_address(message)


def _parse_freeform_address(text: str) -> dict[str, object]:
    """Parse "street[, unit], city, ST ZIP" from the end inwards.

    Working backwards is what makes a variable number of street lines tractable: the
    state and ZIP are recognizable by shape, the city is whatever immediately precedes
    them, the first remaining part is street line 1, and anything left in the middle is
    the optional unit line.
    """
    parts = [part.strip() for part in text.split(",")]
    parts = [part for part in parts if part]
    parsed: dict[str, object] = {}
    if not parts:
        return parsed

    tail = parts.pop()
    tail_tokens = tail.split()
    if len(tail_tokens) >= 2:
        # "IL 62704", and also "Illinois 62704" -- an unabbreviated state is passed
        # through so `validate_address` can say it needs a two-letter code.
        parsed["state"] = " ".join(tail_tokens[:-1])
        parsed["zip_code"] = tail_tokens[-1]
    elif parts and _BARE_ZIP.match(tail):
        # "..., Springfield, IL, 62704": state and ZIP sent as separate parts.
        parsed["state"] = parts.pop()
        parsed["zip_code"] = tail
    else:
        parsed["zip_code"] = tail

    if parts:
        parsed["city"] = parts.pop()
    if parts:
        parsed["street1"] = parts.pop(0)
    if parts:
        parsed["street2"] = " ".join(parts)
    return parsed


def _looks_like_an_address_attempt(message: str) -> bool:
    """True when a non-confirmation reply is structurally an attempt at the requested
    format, so a validation failure should name what was wrong with it.

    A comma is the one structural signal `ADDRESS_PROMPT` actually asks for, and a JSON
    object is the machine-sent shape; anything else ("ok", "hmm", a question) is not a
    correction attempt and gets the review back unannotated instead of a list of
    validation errors it never earned.
    """
    return "," in message or message.strip().startswith("{")


def _rejection_text(details: list[str]) -> str:
    return (
        "That address could not be accepted: "
        + "; ".join(details)
        + f". Please send the whole address again, for example: {_EXAMPLE_ADDRESS}."
    )


def _address_lines(address: Address | ConfirmedAddress) -> str:
    """Render the parsed address as labelled, one-per-line components (AC2).

    Every component is named in plain words on its own line, so the echo-back carries
    no meaning by colour, position, or formatting alone and reads correctly through the
    chat page's `aria-live` transcript (NFR-19). `street2` is shown only when the
    patient actually gave one -- printing an empty "Apartment or unit:" line would
    imply a value they never supplied.
    """
    lines = [f"Street: {address.street1}"]
    if address.street2:
        lines.append(f"Apartment or unit: {address.street2}")
    lines += [
        f"City: {address.city}",
        f"State: {address.state}",
        f"ZIP code: {address.zip_code}",
    ]
    return "\n".join(lines)


def _review_summary(address: Address) -> str:
    return (
        "Here is the address I have for you. Nothing has been saved yet -- please "
        "check it:\n"
        + _address_lines(address)
        + "\n\nReply CONFIRM to save this to your record, send the corrected address to "
        "change it, or reply CANCEL to stop without saving."
    )


def _saved_summary(address: ConfirmedAddress) -> str:
    return (
        "Saved. Your mailing address in your OpenEMR record is now:\n"
        + _address_lines(address)
        + "\n\nYour name and date of birth were not changed. What else can I help you "
        "with?"
    )


@dataclass
class _AddressSessionState:
    """In-process-only state for one session's in-progress address update.

    `pending` doubles as the stage marker: `None` means the flow is still waiting for
    an address, and a set value means that address has been echoed back and is waiting
    on an explicit confirmation. Never persisted (see the module docstring).
    """

    pending: Address | None = None


@dataclass
class AddressChatService:
    """Streams one address-update turn locally, never through Groq."""

    # `None` disables the flow entirely, the same way `OnboardingChatService.flow` does:
    # an environment with no Portal API base URL reports the fixed unavailable message
    # instead of failing startup.
    demographics: OpenEmrDemographicsAdapter | None
    session_store: SessionStore
    clock: Callable[[], datetime] = utc_now
    _sessions: dict[str, _AddressSessionState] = field(default_factory=dict)

    def has_pending_update(self, handle: str) -> bool:
        """True while this session is mid-flow, so the route keeps sending it here.

        The route cannot decide this from SQLite the way `onboarding_mode` reads the
        cursor -- this flow's state is in-process only -- so it asks the service.
        """
        return handle in self._sessions

    def discard(self, handle: str) -> None:
        """Drop any in-progress update for this session without writing anything.

        The route calls this when a turn is handed to guided onboarding instead, so the
        two routes stay unambiguous in both directions: a patient who starts onboarding
        mid-address-update does not silently resume the update afterwards. Nothing was
        written, so there is nothing to undo.
        """
        self._sessions.pop(handle, None)

    async def stream_reply(self, handle: str, message: str) -> AsyncIterator[str]:
        """Yield the fixed unavailable message, or this turn's reply."""
        if self.demographics is None:
            yield UNAVAILABLE_ADDRESS_RESPONSE
            return
        now = self.clock()
        access_token = await asyncio.to_thread(self.session_store.access_token, handle, now)
        if access_token is None:
            yield UNAVAILABLE_ADDRESS_RESPONSE
            return

        # Same precedence onboarding gives distress (`ONBOARDING_CONTRACT.md`): a
        # patient in difficulty gets the approved supportive content, and their message
        # is never also parsed as a bad address. Nothing is written on such a turn, and
        # the flow is still here on the next one.
        distress = detect_distress(message)
        if distress is not None:
            yield SUPPORTIVE_CONTENT[distress]
            return

        state = self._sessions.get(handle)
        if state is None:
            # The triggering turn itself: only ever prompt. The trigger phrase is not
            # parsed as an address, so a patient never has to repeat themselves into a
            # rejection on the very first turn.
            self._sessions[handle] = _AddressSessionState()
            yield ADDRESS_PROMPT
            return

        if is_cancellation(message):
            self._sessions.pop(handle, None)
            yield CANCELLED_RESPONSE
            return

        if state.pending is not None:
            async for chunk in self._handle_confirmation(handle, access_token, state, message):
                yield chunk
            return

        try:
            address = validate_address(parse_address_reply(message))
        except FieldValidationError as exc:
            # Rejected entirely locally, naming the specific component at fault; the
            # session stays in exactly this stage, so the patient corrects it in place
            # rather than restarting the flow (AC5).
            yield _rejection_text(exc.details)
            return
        state.pending = address
        yield _review_summary(address)

    async def _handle_confirmation(
        self,
        handle: str,
        access_token: str,
        state: _AddressSessionState,
        message: str,
    ) -> AsyncIterator[str]:
        assert self.demographics is not None
        pending = state.pending
        assert pending is not None
        if not is_confirmation(message):
            # Not a confirmation: nothing is written, whatever this reply was. A reply
            # that parses as a valid address replaces the pending one and is echoed
            # back for review in its place (AC5); one that was clearly an attempt but
            # failed validation gets the specific reason; anything else just gets the
            # review again. All three re-show the parsed address and write nothing
            # (AC3).
            try:
                corrected = validate_address(parse_address_reply(message))
            except FieldValidationError as exc:
                if _looks_like_an_address_attempt(message):
                    yield _rejection_text(exc.details) + "\n\n" + _review_summary(pending)
                    return
                yield _NOT_CONFIRMED_PREFIX + _review_summary(pending)
                return
            state.pending = corrected
            yield _review_summary(corrected)
            return

        try:
            confirmed = confirm_address(pending)
        except IdentityNotConfirmedError as exc:
            # `validate_address` already passed for `pending`, so this is unreachable in
            # practice; it is handled rather than raised so a write-boundary rule that
            # ever diverges surfaces as a correction prompt, never as a 500 or as a
            # fabricated success.
            yield _rejection_text([str(exc)])
            return
        try:
            await self.demographics.write_confirmed_address(access_token, confirmed)
        except OpenEmrRequestError:
            yield WRITE_FAILED_RESPONSE
            return
        self._sessions.pop(handle, None)
        yield _saved_summary(confirmed)


def unavailable_address_service(
    session_store: SessionStore, clock: Callable[[], datetime] = utc_now
) -> AddressChatService:
    """Return a service that always reports the fixed unavailable message."""
    return AddressChatService(demographics=None, session_store=session_store, clock=clock)
