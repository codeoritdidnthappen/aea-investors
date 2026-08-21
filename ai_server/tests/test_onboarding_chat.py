"""Tests for routing chat turns into `OnboardingFlow` (TICK-035).

Covers the pure mode-routing decision with fake session/cursor state (the ticket's
own "Unit-test the mode-routing decision" requirement), then a full guided-onboarding
conversation end to end through `OnboardingChatService.stream_reply` against a
synthetic OpenEMR (`httpx.MockTransport`, the same discipline `test_onboarding_flow.py`
uses), including a simulated AI-server restart mid-draft (FR-30) and completion.
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

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.main import create_app
from ai_server.app.onboarding_chat import (
    FIELD_PROMPTS,
    OnboardingChatService,
    is_confirmation,
    is_onboarding_start_request,
    onboarding_mode,
    unavailable_onboarding_service,
)
from ai_server.onboarding.draft_client import AssessmentDraftAdapter, OpenEmrPortalSettings
from ai_server.onboarding.flow import OnboardingFlow
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
PORTAL_BASE_URL = "https://openemr.test/apis/default"


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


# --- AC1/AC2: the pure mode-routing decision, with fake session/cursor state -----


def test_no_cursor_and_no_explicit_request_stays_on_scheduling() -> None:
    assert onboarding_mode(None, "Can I book an appointment for next week?") is False


def test_no_cursor_but_an_explicit_start_request_switches_to_onboarding() -> None:
    assert onboarding_mode(None, "I'd like to start onboarding please") is True
    assert onboarding_mode(None, "Start Onboarding") is True
    assert onboarding_mode(None, json.dumps({"action": "start_onboarding"})) is True


def test_a_present_cursor_stays_in_onboarding_regardless_of_message_content() -> None:
    assert onboarding_mode("draft-1", "hello") is True
    assert onboarding_mode("draft-1", "Can I book an appointment instead?") is True


@pytest.mark.parametrize(
    "message",
    [
        "start onboarding",
        "Begin Intake",
        "I want to complete my onboarding",
        "get started with onboarding",
    ],
)
def test_is_onboarding_start_request_matches_the_fixed_phrase_corpus(message: str) -> None:
    assert is_onboarding_start_request(message) is True


def test_is_onboarding_start_request_rejects_unrelated_text() -> None:
    assert is_onboarding_start_request("What time does the clinic open?") is False


def test_is_confirmation_matches_confirm_but_not_other_text() -> None:
    assert is_confirmation("confirm") is True
    assert is_confirmation("Confirm") is True
    assert is_confirmation(json.dumps({"action": "confirm"})) is True
    assert is_confirmation("not yet") is False


# --- Unavailable fallback ---------------------------------------------------------


def test_unavailable_onboarding_service_streams_the_fixed_message(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(
        OAuthTokens("access", "refresh", "nonce"), NOW, configured.session_ttl
    )
    service = unavailable_onboarding_service(store, clock=lambda: NOW)

    async def run() -> list[str]:
        return [chunk async for chunk in service.stream_reply(handle, "start onboarding")]

    assert asyncio.run(run()) == [
        "The guided onboarding assistant is unavailable right now. Please try again "
        "shortly, or ask a member of staff to help you register in person."
    ]


def test_a_missing_or_expired_session_streams_the_unavailable_message(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    server = _SyntheticOpenEmr()
    service = OnboardingChatService(flow=_flow(server), session_store=store, clock=lambda: NOW)

    async def run() -> list[str]:
        return [chunk async for chunk in service.stream_reply("no-such-handle", "start onboarding")]

    assert "unavailable" in asyncio.run(run())[0].lower()


# --- Full conversation, against a synthetic OpenEMR -------------------------------


class _SyntheticOpenEmr:
    """A minimal in-memory stand-in for the draft + demographics endpoints, shared
    across independently constructed adapters so a test can simulate an AI-server
    restart (mirrors `test_onboarding_flow.py`'s own fixture)."""

    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, object]] = {}
        self.demographics_writes: list[httpx.Request] = []
        self._next_id = 1

    def draft_handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/assessment"):
            uuid = f"draft-{self._next_id}"
            self._next_id += 1
            body = _json_body(request)
            self.drafts[uuid] = {"status": "draft", "fields": body}
            return httpx.Response(201, json={"uuid": uuid, "status": "draft", "fields": body})
        uuid = request.url.path.rsplit("/", 1)[-1]
        record = self.drafts.get(uuid)
        if record is None:
            return httpx.Response(404, json={"error": "no assessment draft with that id"})
        if request.method == "GET":
            return httpx.Response(
                200, json={"uuid": uuid, "status": record["status"], "fields": record["fields"]}
            )
        if request.method == "PUT":
            if record["status"] == "completed":
                return httpx.Response(409, json={"error": "this assessment is already completed"})
            body = _json_body(request)
            requested_complete = body.pop("status", None) == "completed"
            record["fields"] = {**record["fields"], **body}
            if requested_complete:
                missing = [
                    key
                    for key in (
                        "preferred_contact_method",
                        "help_type",
                        "visit_format",
                        "visit_time_window",
                    )
                    if not record["fields"].get(key)
                ]
                if missing:
                    return httpx.Response(
                        400,
                        json={
                            "error": "validation failed",
                            "details": [f"{key} is required" for key in missing],
                        },
                    )
                record["status"] = "completed"
            return httpx.Response(
                200, json={"uuid": uuid, "status": record["status"], "fields": record["fields"]}
            )
        raise AssertionError(f"unexpected method {request.method}")

    def demographics_handler(self, request: httpx.Request) -> httpx.Response:
        self.demographics_writes.append(request)
        return httpx.Response(200, json={"data": {}})


def _json_body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content) if request.content else {}


def _flow(server: _SyntheticOpenEmr) -> OnboardingFlow:
    draft_client = httpx.AsyncClient(transport=httpx.MockTransport(server.draft_handler))
    draft_adapter = AssessmentDraftAdapter(
        OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL), draft_client
    )
    demographics_client = httpx.AsyncClient(
        transport=httpx.MockTransport(server.demographics_handler)
    )
    demographics_adapter = OpenEmrDemographicsAdapter(
        OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL), demographics_client
    )
    return OnboardingFlow(draft_adapter, demographics_adapter)


async def _send(service: OnboardingChatService, handle: str, message: str) -> str:
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


def test_full_onboarding_conversation_reaches_a_completed_record(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    server = _SyntheticOpenEmr()

    async def scenario() -> None:
        service = OnboardingChatService(flow=_flow(server), session_store=store, clock=lambda: NOW)

        # Starting the flow creates a draft and prompts for the first draft field.
        reply = await _send(service, handle, "start onboarding")
        assert reply == FIELD_PROMPTS["preferred_contact"]
        assert store.load_cursor(handle, NOW) is not None

        # An invalid field is rejected and never advances the draft.
        rejection = await _send(service, handle, json.dumps({"method": "carrier_pigeon"}))
        assert "could not be accepted" in rejection
        cursor = store.load_cursor(handle, NOW)
        assert server.drafts[cursor]["fields"] == {}

        # A valid field checkpoints and prompts the next one.
        reply = await _send(service, handle, json.dumps({"method": "portal_message"}))
        assert reply == FIELD_PROMPTS["help_type"]
        assert server.drafts[cursor]["fields"]["preferred_contact_method"] == "portal_message"

        # --- Simulate an AI-server restart mid-flow (FR-30): a brand new service,
        # sharing only the durable SessionStore and the synthetic OpenEMR state.
        restarted = OnboardingChatService(
            flow=_flow(server), session_store=store, clock=lambda: NOW
        )
        reply = await _send(restarted, handle, "both")
        assert reply == FIELD_PROMPTS["visit_preference"]
        assert server.drafts[cursor]["fields"]["help_type"] == "both"

        reply = await _send(
            restarted, handle, json.dumps({"format": "video", "time_window": "weekday_morning"})
        )
        assert reply == FIELD_PROMPTS["accommodations"]

        reply = await _send(restarted, handle, json.dumps({"selected": []}))
        assert reply == FIELD_PROMPTS["given_name"]
        assert server.drafts[cursor]["fields"]["accommodations"] == []

        reply = await _send(restarted, handle, "Avery")
        assert reply == FIELD_PROMPTS["family_name"]

        reply = await _send(restarted, handle, "Alden")
        assert reply == FIELD_PROMPTS["date_of_birth"]

        reply = await _send(restarted, handle, "1990-01-01")
        assert reply == FIELD_PROMPTS["address"]

        # An invalid identity field is rejected too.
        rejection = await _send(restarted, handle, json.dumps({"street1": "100 Maple Ave"}))
        assert "could not be accepted" in rejection

        reply = await _send(
            restarted,
            handle,
            json.dumps(
                {
                    "street1": "100 Maple Ave",
                    "city": "Springfield",
                    "state": "IL",
                    "zip_code": "62704",
                }
            ),
        )
        assert "Review your answers" in reply
        assert "Reply CONFIRM to finish" in reply

        # A non-confirmation message re-shows the review instead of completing.
        reply = await _send(restarted, handle, "wait, let me check")
        assert "Review your answers" in reply

        reply = await _send(restarted, handle, "confirm")
        assert "Avery" in reply
        assert "complete" in reply.lower()

        assert server.drafts[cursor]["status"] == "completed"
        assert len(server.demographics_writes) == 1
        assert store.load_cursor(handle, NOW) is None  # cleared after completion

    asyncio.run(scenario())


def test_distress_content_surfaces_through_the_same_streamed_path_and_pauses_the_field(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    server = _SyntheticOpenEmr()

    async def scenario() -> None:
        service = OnboardingChatService(flow=_flow(server), session_store=store, clock=lambda: NOW)
        await _send(service, handle, "start onboarding")

        reply = await _send(service, handle, "I feel overwhelmed")
        assert reply == "I’m sorry this feels difficult. You can pause or continue later."

        # The distress turn was not treated as a field answer.
        cursor = store.load_cursor(handle, NOW)
        assert server.drafts[cursor]["fields"] == {}

    asyncio.run(scenario())


def test_immediate_safety_content_takes_precedence_over_general_distress(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    server = _SyntheticOpenEmr()

    async def scenario() -> None:
        service = OnboardingChatService(flow=_flow(server), session_store=store, clock=lambda: NOW)
        await _send(service, handle, "start onboarding")

        reply = await _send(service, handle, "I want to die")
        assert "988" in reply

    asyncio.run(scenario())


def test_long_pause_supportive_content_shows_once_per_field(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    server = _SyntheticOpenEmr()
    clock_value = NOW

    async def scenario() -> None:
        nonlocal clock_value
        service = OnboardingChatService(
            flow=_flow(server), session_store=store, clock=lambda: clock_value
        )
        await _send(service, handle, "start onboarding")

        clock_value = NOW + timedelta(seconds=130)
        reply = await _send(service, handle, json.dumps({"method": "portal_message"}))
        assert reply.startswith(
            "Take your time. Your progress is saved, and you can continue when you’re ready."
        )

        # A second long gap on the *next* field shows the message again (once per
        # field, not once per session).
        clock_value = clock_value + timedelta(seconds=130)
        reply = await _send(service, handle, "both")
        assert reply.startswith("Take your time.")

    asyncio.run(scenario())


def test_completion_without_a_bound_patient_uuid_still_completes_tick_042(
    tmp_path: Path,
) -> None:
    """A session whose ID token never carried `fhirUser`/`sub` (TICK-028,
    `SessionStore.patient_uuid` is documented best-effort) still completes onboarding
    (TICK-042): the demographics write no longer needs a locally-captured patient id at
    all -- the module route resolves the target patient server-side from the bearer
    token itself, matching `BookingTool`'s own TICK-040 contract."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(minutes=30),
    )
    server = _SyntheticOpenEmr()

    async def scenario() -> None:
        service = OnboardingChatService(flow=_flow(server), session_store=store, clock=lambda: NOW)
        await _send(service, handle, "start onboarding")
        await _send(service, handle, json.dumps({"method": "portal_message"}))
        await _send(service, handle, "both")
        await _send(
            service, handle, json.dumps({"format": "video", "time_window": "weekday_morning"})
        )
        await _send(service, handle, json.dumps({"selected": []}))
        await _send(service, handle, "Avery")
        await _send(service, handle, "Alden")
        await _send(service, handle, "1990-01-01")
        await _send(
            service,
            handle,
            json.dumps(
                {
                    "street1": "100 Maple Ave",
                    "city": "Springfield",
                    "state": "IL",
                    "zip_code": "62704",
                }
            ),
        )
        reply = await _send(service, handle, "confirm")

        assert "complete" in reply.lower()
        assert len(server.demographics_writes) == 1
        cursor = store.load_cursor(handle, NOW)
        assert cursor is None  # cleared after completion

    asyncio.run(scenario())


# --- Route-level dispatch (main.py) ------------------------------------------------


@dataclass
class _ScriptedOnboardingService:
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def stream_reply(self, handle: str, message: str) -> AsyncIterator[str]:
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


def test_route_dispatches_to_onboarding_once_a_cursor_is_active(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(OAuthTokens("a", "r", "n"), NOW, configured.session_ttl)
    store.save_cursor(handle, "draft-1", NOW)

    onboarding = _ScriptedOnboardingService()
    scheduling = _ScriptedSchedulingService()
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=scheduling,
        onboarding_service=onboarding,
    )

    response = asyncio.run(_post_chat(app, handle, "what's the weather"))

    assert response.status_code == 200
    assert response.text == "onboarding-reply"
    assert onboarding.calls == [(handle, "what's the weather")]
    assert scheduling.calls == []


def test_route_keeps_using_scheduling_with_no_cursor_and_no_start_request(
    tmp_path: Path,
) -> None:
    """AC2: no regression to TICK-034's scheduling behavior."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(OAuthTokens("a", "r", "n"), NOW, configured.session_ttl)

    onboarding = _ScriptedOnboardingService()
    scheduling = _ScriptedSchedulingService()
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=scheduling,
        onboarding_service=onboarding,
    )

    response = asyncio.run(_post_chat(app, handle, "Can I book an appointment?"))

    assert response.status_code == 200
    assert response.text == "scheduling-reply"
    assert scheduling.calls == ["Can I book an appointment?"]
    assert onboarding.calls == []


def test_route_dispatches_to_onboarding_on_an_explicit_start_request_with_no_cursor(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(OAuthTokens("a", "r", "n"), NOW, configured.session_ttl)

    onboarding = _ScriptedOnboardingService()
    scheduling = _ScriptedSchedulingService()
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=scheduling,
        onboarding_service=onboarding,
    )

    response = asyncio.run(_post_chat(app, handle, "start onboarding"))

    assert response.status_code == 200
    assert response.text == "onboarding-reply"
    assert onboarding.calls == [(handle, "start onboarding")]
    assert scheduling.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
