"""Tests for the local, confirm-then-write address update in the chat (TICK-050).

Covers the pure routing/trigger decisions, the local freeform address parse, then the
whole turn sequence end to end through `AddressChatService.stream_reply` against a
synthetic OpenEMR (`httpx.MockTransport`, the same discipline
`test_onboarding_chat.py` uses): trigger -> prompt -> invalid address -> correction ->
parsed echo-back -> non-confirmation re-shows the review -> confirmation writes exactly
once. Finally, route-level tests prove the address never reaches Groq, by driving the
real `ChatService`/`GroqWorkflow` with a client that captures every outbound payload.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from ai_server.app.address_chat import (
    ADDRESS_PROMPT,
    CANCELLED_RESPONSE,
    WRITE_FAILED_RESPONSE,
    AddressChatService,
    address_update_mode,
    is_address_update_request,
    is_cancellation,
    is_confirmation,
    parse_address_reply,
    unavailable_address_service,
)
from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.chat import CHAT_PAGE_HTML, ChatService, no_action_tool_factory
from ai_server.app.main import create_app
from ai_server.llm.groq import GroqWorkflow
from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.onboarding.triggers import SUPPORTIVE_CONTENT, Trigger
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter
from ai_server.privacy.gate import OutboundPayload, PrivacyGate

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
PORTAL_BASE_URL = "https://openemr.test/apis/default"

STREET = "100 Maple Ave"
UNIT = "Apt 4B"
CITY = "Springfield"
STATE = "IL"
ZIP_CODE = "62704"
FULL_ADDRESS = f"{STREET}, {UNIT}, {CITY}, {STATE} {ZIP_CODE}"


def settings(tmp_path: Path) -> AuthSettings:
    return AuthSettings(
        database_path=tmp_path / "sessions.sqlite3",
        encryption_key=b"k" * 32,
        authorize_url="https://openemr.test/oauth2/default/authorize",
        token_url="https://openemr.test/oauth2/default/token",
        jwks_url="https://openemr.test/oauth2/default/jwks",
        issuer="https://openemr.test",
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://chat.test/oauth/callback",
        success_redirect_uri="https://chat.test/",
        session_ttl=timedelta(minutes=30),
        state_ttl=timedelta(minutes=5),
    )


# --- AC1: the trigger corpus, and what it must not collide with --------------------


@pytest.mark.parametrize(
    "message",
    [
        "update my address",
        "I'd like to update my address please",
        "Change my address",
        "can you correct my address?",
        "My address has changed",
        "I moved",
        "I've moved",
        "I’ve moved.",
        "we moved",
        "update my mailing address",
    ],
)
def test_is_address_update_request_matches_the_fixed_phrase_corpus(message: str) -> None:
    assert is_address_update_request(message) is True


def test_a_machine_sent_action_also_starts_the_flow() -> None:
    assert is_address_update_request(json.dumps({"action": "update_address"})) is True


@pytest.mark.parametrize(
    "message",
    [
        # Scheduling must be untouched -- "I moved my appointment" is exactly the
        # collision a substring match on "i moved" would have caused.
        "I moved my appointment to Friday",
        "Can I move my appointment?",
        "change my appointment",
        "cancel my appointment",
        "Can I book an appointment for next week?",
        "What time does the clinic open?",
        # Onboarding's own start corpus must not land here either.
        "start onboarding",
        "begin intake",
        "complete my onboarding",
    ],
)
def test_is_address_update_request_rejects_scheduling_and_onboarding_phrases(
    message: str,
) -> None:
    assert is_address_update_request(message) is False


def test_is_confirmation_and_is_cancellation_are_explicit_only() -> None:
    assert is_confirmation("confirm") is True
    assert is_confirmation("Confirm.") is True
    assert is_confirmation("Yes") is True
    assert is_confirmation(json.dumps({"action": "confirm_address_update"})) is True
    assert is_confirmation("ok maybe") is False
    assert is_confirmation("not yet") is False

    assert is_cancellation("cancel") is True
    assert is_cancellation("Never mind!") is True
    assert is_cancellation(json.dumps({"action": "cancel_address_update"})) is True
    assert is_cancellation("cancel my appointment") is False


# --- AC8: mode routing, and the two-way no-hijack rule -----------------------------


def test_no_request_and_nothing_in_progress_stays_on_scheduling() -> None:
    assert address_update_mode(None, False, "Can I book an appointment?") is False


def test_an_explicit_request_switches_modes() -> None:
    assert address_update_mode(None, False, "update my address") is True


def test_an_in_progress_update_keeps_every_further_turn() -> None:
    assert address_update_mode(None, True, "100 Maple Ave, Springfield, IL 62704") is True
    assert address_update_mode(None, True, "confirm") is True


def test_an_onboarding_cursor_always_wins(tmp_path: Path) -> None:
    """AC8: a patient mid-onboarding is never hijacked into this flow."""
    del tmp_path
    assert address_update_mode("draft-1", False, "update my address") is False
    assert address_update_mode("draft-1", True, "update my address") is False


# --- AC2/AC5: the local freeform parse ---------------------------------------------


def test_parse_reads_a_plain_comma_separated_address() -> None:
    assert parse_address_reply(f"{STREET}, {CITY}, {STATE} {ZIP_CODE}") == {
        "street1": STREET,
        "city": CITY,
        "state": STATE,
        "zip_code": ZIP_CODE,
    }


def test_parse_reads_an_optional_unit_line() -> None:
    assert parse_address_reply(FULL_ADDRESS) == {
        "street1": STREET,
        "street2": UNIT,
        "city": CITY,
        "state": STATE,
        "zip_code": ZIP_CODE,
    }


def test_parse_reads_a_state_and_zip_sent_as_separate_parts() -> None:
    assert parse_address_reply(f"{STREET}, {CITY}, {STATE}, {ZIP_CODE}") == {
        "street1": STREET,
        "city": CITY,
        "state": STATE,
        "zip_code": ZIP_CODE,
    }


def test_parse_keeps_an_unabbreviated_state_so_validation_can_name_it() -> None:
    """A spelled-out state must not be silently promoted into the ZIP slot -- the
    patient gets told the state needs a two-letter code, which is the real problem."""
    assert parse_address_reply(f"{STREET}, {CITY}, Illinois 62704") == {
        "street1": STREET,
        "city": CITY,
        "state": "Illinois",
        "zip_code": ZIP_CODE,
    }


def test_parse_still_accepts_onboardings_json_shape() -> None:
    body = {"street1": STREET, "city": CITY, "state": STATE, "zip_code": ZIP_CODE}
    assert parse_address_reply(json.dumps(body)) == body


def test_parse_never_raises_on_junk() -> None:
    assert isinstance(parse_address_reply("hello"), dict)
    assert isinstance(parse_address_reply(""), dict)


# --- Unavailable fallback -----------------------------------------------------------


def test_unavailable_address_service_streams_the_fixed_message(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    service = unavailable_address_service(store, clock=lambda: NOW)

    assert "unavailable" in asyncio.run(_send(service, handle, "update my address")).lower()


def test_a_missing_or_expired_session_streams_the_unavailable_message(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    service, _ = _service(store)

    assert "unavailable" in asyncio.run(_send(service, "no-such-handle", "I moved")).lower()


# --- The conversation, against a synthetic OpenEMR ----------------------------------


class _SyntheticOpenEmr:
    """A minimal in-memory stand-in for the demographics write endpoint (mirrors
    `test_onboarding_chat.py`'s own fixture), able to fail on demand."""

    def __init__(self, status_code: int = 200) -> None:
        self.demographics_writes: list[httpx.Request] = []
        self.status_code = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.demographics_writes.append(request)
        return httpx.Response(self.status_code, json={"data": {}})

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.demographics_writes]


def _service(
    store: SessionStore, status_code: int = 200
) -> tuple[AddressChatService, _SyntheticOpenEmr]:
    server = _SyntheticOpenEmr(status_code)
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    adapter = OpenEmrDemographicsAdapter(
        OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL), client
    )
    return (
        AddressChatService(demographics=adapter, session_store=store, clock=lambda: NOW),
        server,
    )


async def _send(service: AddressChatService, handle: str, message: str) -> str:
    return "".join([chunk async for chunk in service.stream_reply(handle, message)])


def _bound_session(store: SessionStore, ttl: timedelta = timedelta(minutes=30)) -> str:
    return store.create_session(
        OAuthTokens(
            "synthetic-access-token",
            "synthetic-refresh-token",
            "nonce",
            patient_uuid="synthetic-patient-uuid",
        ),
        NOW,
        ttl,
    )


def _store(tmp_path: Path) -> SessionStore:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    return store


def test_the_full_turn_sequence_writes_exactly_once_on_confirmation(tmp_path: Path) -> None:
    """AC1-AC5 end to end: trigger, prompt, invalid, correction, echo-back,
    non-confirmation re-shows the review, confirmation writes once."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        # AC1: a plain request is prompted for the address.
        prompt = await _send(service, handle, "I moved")
        assert prompt == ADDRESS_PROMPT
        assert server.demographics_writes == []

        # AC5: an invalid address is rejected locally, naming the specific problem.
        rejected = await _send(service, handle, f"{STREET}, {CITY}, ZZ 1234")
        assert "state must be a two-letter US state or territory code" in rejected
        assert "zip_code must be a five- or nine-digit US ZIP code" in rejected
        assert server.demographics_writes == []

        # AC5: corrected in place, without restarting the flow.
        review = await _send(service, handle, FULL_ADDRESS)
        # AC2: echoed back structured and human-readable, component by component.
        assert f"Street: {STREET}" in review
        assert f"Apartment or unit: {UNIT}" in review
        assert f"City: {CITY}" in review
        assert f"State: {STATE}" in review
        assert f"ZIP code: {ZIP_CODE}" in review
        assert "CONFIRM" in review
        assert server.demographics_writes == []

        # AC3: a non-confirmation re-shows the parsed address and writes nothing.
        for not_a_confirmation in ("ok", "hmm", "what happens next?"):
            again = await _send(service, handle, not_a_confirmation)
            assert f"Street: {STREET}" in again
            assert f"ZIP code: {ZIP_CODE}" in again
            assert server.demographics_writes == []

        # AC4: on confirmation the chat writes it itself and reports the real outcome.
        saved = await _send(service, handle, "confirm")
        assert "Saved" in saved
        assert f"City: {CITY}" in saved

    asyncio.run(scenario())

    # AC4: exactly one write, address-only and structured (TICK-049's path).
    assert len(server.demographics_writes) == 1
    request = server.demographics_writes[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/portal/patient/demographics")
    assert request.headers["authorization"] == "Bearer synthetic-access-token"
    assert server.bodies()[0] == {
        "street": STREET,
        "street_line_2": UNIT,
        "city": CITY,
        "state": STATE,
        "postal_code": ZIP_CODE,
    }


def test_the_write_never_touches_name_or_date_of_birth(tmp_path: Path) -> None:
    """AC4/TICK-049: an address-only body means OpenEMR's UPDATE names only address
    columns, so name and date of birth cannot be blanked by this flow."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        await _send(service, handle, f"{STREET}, {CITY}, {STATE} {ZIP_CODE}")
        await _send(service, handle, "confirm")

    asyncio.run(scenario())

    body = server.bodies()[0]
    assert set(body) == {"street", "street_line_2", "city", "state", "postal_code"}
    for untouched in ("fname", "lname", "DOB"):
        assert untouched not in body


def test_a_correction_at_the_review_stage_replaces_the_pending_address(tmp_path: Path) -> None:
    """AC3/AC5: sending a different address at review re-shows the new one and still
    writes nothing until it is confirmed."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        await _send(service, handle, FULL_ADDRESS)
        corrected = await _send(service, handle, "42 Oak Street, Austin, TX 78701")
        assert "Street: 42 Oak Street" in corrected
        assert STREET not in corrected
        # The unit line from the superseded address must not survive into the new one.
        assert "Apartment or unit" not in corrected
        assert server.demographics_writes == []
        await _send(service, handle, "yes")

    asyncio.run(scenario())

    assert server.bodies() == [
        {
            "street": "42 Oak Street",
            "street_line_2": "",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
        }
    ]


def test_an_invalid_correction_at_review_explains_itself_and_re_shows_the_review(
    tmp_path: Path,
) -> None:
    """AC3 + AC5 together: the reason is given *and* the parsed address comes back."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        await _send(service, handle, FULL_ADDRESS)
        reply = await _send(service, handle, f"{STREET}, {CITY}, XX {ZIP_CODE}")
        assert "state must be a two-letter US state or territory code" in reply
        assert f"Street: {STREET}" in reply
        assert server.demographics_writes == []

    asyncio.run(scenario())


def test_a_failed_write_is_reported_as_a_failure_and_can_be_retried(tmp_path: Path) -> None:
    """AC4/TICK-041: a failed write is never described as success."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store, status_code=500)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        await _send(service, handle, FULL_ADDRESS)
        failed = await _send(service, handle, "confirm")
        assert failed == WRITE_FAILED_RESPONSE
        assert "saved" not in failed.lower().replace("couldn't save", "")
        # The pending address survives, so confirming again retries without retyping.
        server.status_code = 200
        retried = await _send(service, handle, "confirm")
        assert "Saved" in retried

    asyncio.run(scenario())

    assert len(server.demographics_writes) == 2
    assert server.bodies()[1]["street"] == STREET


def test_abandoning_the_flow_writes_nothing_and_returns_to_normal_chat(tmp_path: Path) -> None:
    """AC7."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        await _send(service, handle, FULL_ADDRESS)
        assert await _send(service, handle, "never mind") == CANCELLED_RESPONSE
        assert server.demographics_writes == []
        # Back to normal chat: the route no longer claims this session's turns.
        assert service.has_pending_update(handle) is False
        assert address_update_mode(None, False, "Can I book an appointment?") is False

    asyncio.run(scenario())


def test_abandoning_before_giving_an_address_also_leaves_nothing_behind(tmp_path: Path) -> None:
    """AC7: the flow can be left at the prompt stage too."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        assert await _send(service, handle, "cancel") == CANCELLED_RESPONSE

    asyncio.run(scenario())

    assert service.has_pending_update(handle) is False
    assert server.demographics_writes == []


def test_distress_gets_the_approved_supportive_content_not_an_address_rejection(
    tmp_path: Path,
) -> None:
    """Parity with onboarding: a patient in difficulty is never answered with a
    validation error, and nothing is written on such a turn."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        reply = await _send(service, handle, "this is too much")
        assert reply == SUPPORTIVE_CONTENT[Trigger.GENERAL_DISTRESS]
        assert server.demographics_writes == []
        # The flow is still here, so the patient can carry on when ready.
        assert "Street" in await _send(service, handle, FULL_ADDRESS)

    asyncio.run(scenario())


def test_discard_drops_an_in_progress_update_without_writing(tmp_path: Path) -> None:
    """AC8's other direction: onboarding taking the turn leaves no partial write."""
    store = _store(tmp_path)
    handle = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, handle, "update my address")
        await _send(service, handle, FULL_ADDRESS)
        assert service.has_pending_update(handle) is True
        service.discard(handle)
        assert service.has_pending_update(handle) is False
        assert server.demographics_writes == []

    asyncio.run(scenario())


def test_two_sessions_do_not_share_a_pending_address(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _bound_session(store)
    second = _bound_session(store)
    service, server = _service(store)

    async def scenario() -> None:
        await _send(service, first, "update my address")
        await _send(service, first, FULL_ADDRESS)
        assert service.has_pending_update(second) is False
        # The second session's own trigger starts at the prompt, not at first's review.
        assert await _send(service, second, "I moved") == ADDRESS_PROMPT
        await _send(service, second, "42 Oak Street, Austin, TX 78701")
        await _send(service, second, "confirm")

    asyncio.run(scenario())

    assert [body["street"] for body in server.bodies()] == ["42 Oak Street"]


# --- AC9: the echo-back's structure has to survive to the screen --------------------


def test_the_chat_page_preserves_the_review_line_breaks() -> None:
    """AC2/AC9: the review puts each address component on its own labelled line, and a
    reply is inserted into the transcript with `textContent`, so without an explicit
    `white-space` rule HTML collapses every newline into a space and the whole review
    renders as one run-on line."""
    assert "#chat-transcript li .message-body { white-space: pre-wrap; }" in CHAT_PAGE_HTML
    # The class the rule targets is the one `appendMessage` actually assigns.
    assert 'body.className = "message-body";' in CHAT_PAGE_HTML


# --- Route-level dispatch, and the AC6 no-Groq proof --------------------------------


@dataclass
class _ScriptedOnboardingService:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def stream_reply(
        self, handle: str, message: str, image_base64: str | None = None
    ) -> AsyncIterator[str]:
        del image_base64
        self.calls.append((handle, message))
        yield "onboarding-reply"


@dataclass
class _ScriptedSchedulingService:
    calls: list[str] = field(default_factory=list)

    async def stream_reply(
        self, message: str, access_token: str | None = None, patient_id: str | None = None
    ) -> AsyncIterator[str]:
        del access_token, patient_id
        self.calls.append(message)
        yield "scheduling-reply"


class _CapturingGroqClient:
    """Records every payload that would leave this process for Groq (mirrors
    `test_groq.py`'s own capturing client -- this file stays self-contained)."""

    def __init__(self) -> None:
        self.calls: list[OutboundPayload] = []

    async def complete(self, request: OutboundPayload) -> str:
        self.calls.append(request)
        return '{"intent":"information","slot_token":null}'

    async def _stream(self, request: OutboundPayload) -> AsyncIterator[str]:
        self.calls.append(request)
        yield "scheduling-reply"

    def stream(self, request: OutboundPayload) -> AsyncIterator[str]:
        return self._stream(request)


async def _post_chat(app, cookie: str, message: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie},
        ) as client:
            return await client.post(
                "/api/chat", json={"message": message}, headers={"origin": "https://chat.test"}
            )


async def _post_all(app, cookie: str, messages: list[str]) -> list[str]:
    """Drive a whole conversation through one app instance, keeping the in-memory
    address session alive across turns (a fresh lifespan per turn would not)."""
    replies: list[str] = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie},
        ) as client:
            for message in messages:
                response = await client.post(
                    "/api/chat",
                    json={"message": message},
                    headers={"origin": "https://chat.test"},
                )
                assert response.status_code == 200
                replies.append(response.text)
    return replies


def test_route_dispatches_an_address_request_away_from_scheduling(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    service, _ = _service(store)
    scheduling = _ScriptedSchedulingService()
    app = create_app(
        configured, clock=lambda: NOW, chat_service=scheduling, address_service=service
    )

    response = asyncio.run(_post_chat(app, handle, "update my address"))

    assert response.status_code == 200
    assert response.text == ADDRESS_PROMPT
    assert scheduling.calls == []


def test_route_keeps_scheduling_for_everything_else(tmp_path: Path) -> None:
    """No regression: an ordinary scheduling turn still reaches the Groq-backed path."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    service, _ = _service(store)
    scheduling = _ScriptedSchedulingService()
    app = create_app(
        configured, clock=lambda: NOW, chat_service=scheduling, address_service=service
    )

    response = asyncio.run(_post_chat(app, handle, "Can I book an appointment?"))

    assert response.text == "scheduling-reply"
    assert scheduling.calls == ["Can I book an appointment?"]
    assert service.has_pending_update(handle) is False


def test_route_leaves_an_onboarding_session_alone(tmp_path: Path) -> None:
    """AC8: a patient mid-onboarding saying "update my address" stays in onboarding."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    store.save_cursor(handle, "draft-1", NOW)
    service, server = _service(store)
    onboarding = _ScriptedOnboardingService()
    scheduling = _ScriptedSchedulingService()
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=scheduling,
        onboarding_service=onboarding,
        address_service=service,
    )

    response = asyncio.run(_post_chat(app, handle, "update my address"))

    assert response.text == "onboarding-reply"
    assert onboarding.calls == [(handle, "update my address")]
    assert scheduling.calls == []
    assert service.has_pending_update(handle) is False
    assert server.demographics_writes == []


def test_route_drops_an_in_progress_update_when_onboarding_takes_the_turn(
    tmp_path: Path,
) -> None:
    """AC8's other direction, at the route: starting onboarding mid-update abandons the
    update outright rather than leaving it to resume later, and writes nothing."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    service, server = _service(store)
    onboarding = _ScriptedOnboardingService()
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=_ScriptedSchedulingService(),
        onboarding_service=onboarding,
        address_service=service,
    )

    replies = asyncio.run(
        _post_all(app, handle, ["update my address", FULL_ADDRESS, "start onboarding"])
    )

    assert replies[2] == "onboarding-reply"
    assert service.has_pending_update(handle) is False
    assert server.demographics_writes == []


def test_the_address_never_reaches_groq(tmp_path: Path) -> None:
    """AC6, proven by inspecting outbound payloads.

    The route is driven with the *real* `ChatService`/`GroqWorkflow`, whose client
    records every payload that would leave this process. The whole address conversation
    must produce none; the control turn afterwards proves the recorder works and would
    have caught one.
    """
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    service, server = _service(store)

    groq_client = _CapturingGroqClient()
    chat_service = ChatService(
        workflow=GroqWorkflow(PrivacyGate.create(), groq_client),
        tool_factory=no_action_tool_factory(),
        clock=lambda: NOW,
    )
    app = create_app(
        configured, clock=lambda: NOW, chat_service=chat_service, address_service=service
    )

    conversation = [
        "I moved",
        f"{STREET}, {CITY}, ZZ 1234",  # a rejected attempt still must not leak
        FULL_ADDRESS,
        "ok",
        "confirm",
    ]
    replies = asyncio.run(_post_all(app, handle, conversation))

    assert "Saved" in replies[-1]
    assert len(server.demographics_writes) == 1
    # Not one outbound Groq payload was built at all during the address flow.
    assert groq_client.calls == []

    # Control: an ordinary scheduling turn does reach Groq, so the assertion above is
    # a real observation rather than a broken harness. (The message is kept free of
    # anything Presidio flags -- "next week" alone is a DATE_TIME and would be stopped
    # by the gate before the client, which would make this control prove nothing.)
    asyncio.run(_post_all(app, handle, ["Can I book an appointment?"]))
    assert groq_client.calls != []
    for payload in groq_client.calls:
        serialized = payload.model_dump_json()
        for secret in (STREET, UNIT, CITY, ZIP_CODE):
            assert secret not in serialized
