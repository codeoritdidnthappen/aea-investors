"""Tests for routing chat turns into `OnboardingFlow` (TICK-035).

Covers the pure mode-routing decision with fake session/cursor state (the ticket's
own "Unit-test the mode-routing decision" requirement), then a full guided-onboarding
conversation end to end through `OnboardingChatService.stream_reply` against a
synthetic OpenEMR (`httpx.MockTransport`, the same discipline `test_onboarding_flow.py`
uses), including a simulated AI-server restart mid-draft (FR-30) and completion.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import struct
import zlib
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
from ai_server.ocr.service import (
    MAX_UPLOAD_BYTES,
    OcrService,
    SubprocessTesseractEngine,
    TesseractUnavailableError,
)
from ai_server.onboarding.draft_client import AssessmentDraftAdapter, OpenEmrPortalSettings
from ai_server.onboarding.flow import OnboardingFlow
from ai_server.onboarding.triggers import SUPPORTIVE_CONTENT, Trigger
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


async def _send(
    service: OnboardingChatService, handle: str, message: str, image_base64: str | None = None
) -> str:
    return "".join([chunk async for chunk in service.stream_reply(handle, message, image_base64)])


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


def test_tick_043_an_empty_family_name_chat_turn_is_rejected_not_accepted_as_a_mononym(
    tmp_path: Path,
) -> None:
    """A patient answering "What is your legal family (last) name?" with an empty
    string is rejected at that same turn, never advances the identity capture, and
    never reaches confirm_identity()/OpenEMR at all -- the actual chat-turn path
    TICK-043's fix covers, not just confirm_identity() called directly."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
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
        await _send(service, handle, "Cher")

        rejection = await _send(service, handle, "")

        assert "could not be accepted" in rejection
        assert len(server.demographics_writes) == 0

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


# --- TICK-044: consented OCR identity upload wired into the given_name prompt -----

_CARD_TEXT = (
    "SYNTHETIC DEMO ID\n"
    "NAME: Avery Alden\n"
    "DOB: 1990-01-01\n"
    "ADDRESS: 100 Maple Avenue, Austin, TX 78701\n"
    "ID: SYN-00000001\n"
)


def _png_bytes(width: int = 4, height: int = 2) -> bytes:
    """Build a minimal, structurally valid grayscale PNG without any dependency
    (mirrors `test_ocr_service.py`'s own helper -- this file stays self-contained)."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data))
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([255]) * width for _ in range(height))
    idat = zlib.compress(raw)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class _FakeOcrEngine:
    """A `TesseractEngine`-shaped double: never shells out to real Tesseract."""

    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[bytes] = []

    async def recognize_text(self, image: bytes) -> str:
        self.calls.append(image)
        if self.fail:
            raise TesseractUnavailableError("engine unavailable")
        return self.text


class _RecordingOcrService(OcrService):
    """Records the upload id `begin()` issues, so a test can prove it was purged."""

    def __init__(self, engine: _FakeOcrEngine) -> None:
        super().__init__(engine)
        self.last_upload_id: str | None = None

    def begin(self, *, consent: bool, now: datetime) -> str:
        upload_id = super().begin(consent=consent, now=now)
        self.last_upload_id = upload_id
        return upload_id


def _upload_action(*, consent: bool = True) -> str:
    return json.dumps({"action": "upload_identity_document", "consent": consent})


def _image_base64(image: bytes) -> str:
    return base64.b64encode(image).decode("ascii")


async def _advance_to_given_name(service: OnboardingChatService, handle: str) -> str:
    """Answer every draft field so the flow reaches the `given_name` prompt -- the
    only point an upload is accepted (TICK-044 design decision #2)."""
    await _send(service, handle, "start onboarding")
    await _send(service, handle, json.dumps({"method": "portal_message"}))
    await _send(service, handle, "both")
    await _send(service, handle, json.dumps({"format": "video", "time_window": "weekday_morning"}))
    return await _send(service, handle, json.dumps({"selected": []}))


def _upload_service(
    tmp_path: Path, engine: _FakeOcrEngine | None = None, ocr: OcrService | None = None
) -> tuple[OnboardingChatService, str, _SyntheticOpenEmr]:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = _bound_session(store)
    server = _SyntheticOpenEmr()
    resolved_ocr = ocr if ocr is not None else OcrService(engine or _FakeOcrEngine())
    service = OnboardingChatService(
        flow=_flow(server), session_store=store, ocr=resolved_ocr, clock=lambda: NOW
    )
    return service, handle, server


def test_tick_044_the_given_name_prompt_mentions_the_upload_option() -> None:
    assert "attach a photo of your ID" in FIELD_PROMPTS["given_name"]


def test_tick_044_upload_without_explicit_consent_is_refused_and_reprompts(
    tmp_path: Path,
) -> None:
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    service, handle, _ = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        prompt = await _advance_to_given_name(service, handle)
        assert prompt == FIELD_PROMPTS["given_name"]

        reply = await _send(
            service, handle, json.dumps({"action": "upload_identity_document", "consent": False})
        )

        assert SUPPORTIVE_CONTENT[Trigger.UPLOAD_FAILURE] in reply
        assert FIELD_PROMPTS["given_name"] in reply
        assert engine.calls == []  # consent was refused before OCR ever ran

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad_image",
    [
        b"not an image at all",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 4,
        b"\x00" * (MAX_UPLOAD_BYTES + 1),
    ],
    ids=["unsupported_format", "corrupt", "oversized"],
)
def test_tick_044_each_invalid_upload_subtype_is_rejected_with_a_clear_retry_message(
    tmp_path: Path, bad_image: bytes
) -> None:
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    service, handle, _ = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)

        reply = await _send(service, handle, _upload_action(), _image_base64(bad_image))

        assert SUPPORTIVE_CONTENT[Trigger.UPLOAD_FAILURE] in reply
        assert FIELD_PROMPTS["given_name"] in reply
        assert engine.calls == []  # invalid uploads never reach the OCR engine

    asyncio.run(scenario())


def test_tick_044_a_missing_or_non_base64_image_payload_is_rejected_with_a_clear_retry_message(
    tmp_path: Path,
) -> None:
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    service, handle, _ = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)

        missing_image_reply = await _send(service, handle, _upload_action())
        assert SUPPORTIVE_CONTENT[Trigger.UPLOAD_FAILURE] in missing_image_reply

        bad_base64_reply = await _send(service, handle, _upload_action(), "not valid base64!!")
        assert SUPPORTIVE_CONTENT[Trigger.UPLOAD_FAILURE] in bad_base64_reply
        assert engine.calls == []  # neither malformed payload ever reached the OCR engine

    asyncio.run(scenario())


def test_tick_044_a_tesseract_unavailable_empty_result_is_a_clear_retryable_rejection(
    tmp_path: Path,
) -> None:
    engine = _FakeOcrEngine(fail=True)
    service, handle, _ = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)

        reply = await _send(service, handle, _upload_action(), _image_base64(_png_bytes()))

        assert SUPPORTIVE_CONTENT[Trigger.UPLOAD_FAILURE] in reply
        assert FIELD_PROMPTS["given_name"] in reply
        assert len(engine.calls) == 1  # the engine did run; it just extracted nothing

    asyncio.run(scenario())


def test_tick_044_a_successful_upload_purges_the_image_and_extraction_immediately(
    tmp_path: Path,
) -> None:
    """AC4: a second `identity()`/`image()` call on the same upload id returns `None`
    once the upload turn that consumed it has completed (NFR-23)."""
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    ocr = _RecordingOcrService(engine)
    service, handle, _ = _upload_service(tmp_path, ocr=ocr)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)

        reply = await _send(service, handle, _upload_action(), _image_base64(_png_bytes()))

        assert "Avery Alden" in reply
        upload_id = ocr.last_upload_id
        assert upload_id is not None
        assert ocr.identity(upload_id, NOW) is None
        assert ocr.image(upload_id, NOW) is None

    asyncio.run(scenario())


def test_tick_044_successful_extraction_offers_hints_on_every_identity_prompt(
    tmp_path: Path,
) -> None:
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    service, handle, _ = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)

        given_name_reply = await _send(
            service, handle, _upload_action(), _image_base64(_png_bytes())
        )
        assert "We read your given name as 'Avery Alden'" in given_name_reply
        assert FIELD_PROMPTS["given_name"] in given_name_reply

        family_name_reply = await _send(service, handle, "Avery")
        assert "We read your family name as 'Avery Alden'" in family_name_reply

        dob_reply = await _send(service, handle, "Alden")
        assert "We read your date of birth as '1990-01-01'" in dob_reply

        address_reply = await _send(service, handle, "1990-01-01")
        assert "We read your address as '100 Maple Avenue, Austin, TX 78701'" in address_reply

    asyncio.run(scenario())


def test_tick_044_only_the_patients_own_typed_reply_is_written_never_the_extracted_value(
    tmp_path: Path,
) -> None:
    """AC2: the patient replies with different values than the upload extracted; the
    corrected values -- not the extracted ones -- are what onboarding completes with,
    proving there is no path from an unconfirmed extracted value to a write."""
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    service, handle, server = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)
        await _send(
            service, handle, _upload_action(), _image_base64(_png_bytes())
        )  # extracts "Avery Alden"

        # The patient types corrections that differ from every extracted suggestion.
        await _send(service, handle, "Jordan")
        await _send(service, handle, "Rivers")
        await _send(service, handle, "1985-05-05")
        reply = await _send(
            service,
            handle,
            json.dumps(
                {
                    "street1": "200 Cedar Street",
                    "city": "Chicago",
                    "state": "IL",
                    "zip_code": "60601",
                }
            ),
        )
        assert "Jordan" in reply and "Rivers" in reply  # review summary shows the typed values
        assert "Avery" not in reply and "Alden" not in reply

        reply = await _send(service, handle, "confirm")

        assert "Thanks, Jordan!" in reply
        assert len(server.demographics_writes) == 1
        written = json.loads(server.demographics_writes[0].content)
        written_text = json.dumps(written)
        assert "Jordan" in written_text
        assert "Avery" not in written_text and "Alden" not in written_text

    asyncio.run(scenario())


def test_tick_044_the_no_upload_manual_entry_path_is_unaffected(tmp_path: Path) -> None:
    """AC5/AC6: a patient who never attaches a file sees exactly today's flow -- the
    upload feature is additive only, never a required step."""
    engine = _FakeOcrEngine(text=_CARD_TEXT)
    service, handle, server = _upload_service(tmp_path, engine)

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)
        reply = await _send(service, handle, "Avery")
        assert reply == FIELD_PROMPTS["family_name"]  # no hint text: no upload occurred
        assert engine.calls == []

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed locally")
def test_tick_044_real_tesseract_processes_an_uploaded_identity_photo_end_to_end(
    tmp_path: Path,
) -> None:
    """AC1: runs the real `SubprocessTesseractEngine` (never `_FakeOcrEngine`) through
    the same chat-turn path a live upload takes, proving the OCR/onboarding plumbing
    genuinely reaches local Tesseract end to end. Field-level extraction accuracy
    against real ID imagery remains TICK-015's separate golden-set gate (Out of
    Scope here); this only proves the turn never crashes and still guides the patient
    back to `given_name` either way."""
    service, handle, _ = _upload_service(tmp_path, ocr=OcrService(SubprocessTesseractEngine()))

    async def scenario() -> None:
        await _advance_to_given_name(service, handle)

        reply = await _send(
            service, handle, _upload_action(), _image_base64(_png_bytes(width=64, height=32))
        )

        assert FIELD_PROMPTS["given_name"] in reply

    asyncio.run(scenario())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
