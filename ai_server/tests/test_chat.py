"""Tests for the embedded chat page and its streaming turn endpoint (TICK-013)."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.chat import ASSISTANT_UNAVAILABLE_RESPONSE, CHAT_PAGE_HTML
from ai_server.app.main import create_app
from ai_server.app.model_turn import (
    GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE,
    unavailable_model_turn_service,
)
from ai_server.ocr.service import MAX_UPLOAD_BYTES

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


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
        dashboard_redirect_uri="https://emr.test/portal/home.php",
        chat_origin="https://chat.test",
        session_ttl=timedelta(minutes=30),
        state_ttl=timedelta(minutes=5),
        # These suites assert exact reply text against a deliberately short
        # 30-minute test TTL, which is inside TICK-055's default 30-minute
        # expiry warning window -- so every turn here would carry the notice.
        # Production's TTL is 8 hours; disable the warning rather than restate
        # it in assertions that are about something else.
        expiry_warning_window=timedelta(0),
    )


def active_session_cookie(configured: AuthSettings) -> str:
    """Create a durable session directly, mirroring a completed OAuth callback."""
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    tokens = OAuthTokens("synthetic-access", "synthetic-refresh", "synthetic-nonce")
    return store.create_session(tokens, NOW, configured.session_ttl)


@dataclass
class ScriptedTurnService:
    """A `ModelTurnService`-shaped double that yields fixed chunks with real async gaps.

    Since TICK-063 the route hands every turn to the model-first turn service, so this
    is the shape the route calls: `(handle, message, image_base64, access_token,
    patient_id)`. The Groq-backed `ChatService` was deleted outright by TICK-064.
    """

    chunks: list[str]

    async def stream_reply(
        self,
        handle: str,
        message: str,
        image_base64: str | None = None,
        access_token: str | None = None,
        patient_id: str | None = None,
    ) -> AsyncIterator[str]:
        del handle, message, image_base64, access_token, patient_id
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    def discard(self, handle: str) -> None:
        del handle


async def _post_chat(
    app,
    cookie: str | None,
    message: str = "Hello",
    origin: str | None = "https://chat.test",
    image_base64: str | None = None,
) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie} if cookie else None,
        ) as client:
            headers = {"origin": origin} if origin else None
            body: dict[str, object] = {"message": message}
            if image_base64 is not None:
                body["image_base64"] = image_base64
            return await client.post("/api/chat", json=body, headers=headers)


def test_tick_044_message_length_cap_is_unwidened_by_the_separate_image_field(
    tmp_path: Path,
) -> None:
    """TICK-044 review finding: image_base64 must never widen the `message` cap that
    every other chat turn (scheduling included) shares -- an oversized image belongs
    only in its own field, capped separately at ai_server.ocr.service.MAX_UPLOAD_BYTES'
    base64-inflated size."""
    app = create_app(settings(tmp_path), clock=lambda: NOW)

    oversized_message = asyncio.run(_post_chat(app, cookie=None, message="x" * 4_001))
    assert oversized_message.status_code == 422

    oversized_image = asyncio.run(
        _post_chat(app, cookie=None, image_base64="x" * (MAX_UPLOAD_BYTES * 2))
    )
    assert oversized_image.status_code == 422

    within_cap_image = asyncio.run(_post_chat(app, cookie=None, image_base64="x" * 1_000))
    assert within_cap_image.status_code != 422  # rejected later (no session), not by shape


# --- AC1: the UI only ever talks to this server's own AI-session-gated route -----


def test_ac1_chat_page_only_fetches_the_ai_servers_own_relative_endpoint() -> None:
    scripts = re.findall(r"<script>(.*?)</script>", CHAT_PAGE_HTML, re.S)
    assert scripts, "chat page must ship its own script"
    script = scripts[0]

    assert 'fetch("/api/chat"' in script
    assert script.count("fetch(") == 1
    assert 'credentials: "include"' in script
    # No absolute origin appears anywhere in the shipped script, so it cannot be made
    # to call OpenEMR or any other host directly (FR-4).
    assert "http://" not in script
    assert "https://" not in script


def test_ac1_chat_turn_requires_an_active_ai_session_cookie(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), clock=lambda: NOW)

    missing_cookie = asyncio.run(_post_chat(app, cookie=None))
    assert missing_cookie.status_code == 401

    bogus_cookie = asyncio.run(_post_chat(app, cookie="not-a-real-handle"))
    assert bogus_cookie.status_code == 401


def test_ac1_chat_turn_rejects_a_missing_or_mismatched_origin(tmp_path: Path) -> None:
    # The session cookie is SameSite=None (required for the cross-site portal
    # iframe), so a matching Origin is the only thing standing between this route
    # and a forged cross-site request; a CORS-simple request needs no preflight
    # and Starlette parses the body as JSON regardless of Content-Type.
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    app = create_app(
        configured,
        clock=lambda: NOW,
        model_turn_service=ScriptedTurnService(chunks=["Hel", "lo!"]),
    )

    missing_origin = asyncio.run(_post_chat(app, cookie=handle, origin=None))
    assert missing_origin.status_code == 403

    wrong_origin = asyncio.run(_post_chat(app, cookie=handle, origin="https://attacker.test"))
    assert wrong_origin.status_code == 403


def test_ac1_chat_turn_origin_check_is_case_insensitive(tmp_path: Path) -> None:
    # Real browsers always send Origin lowercased; a config value with any
    # uppercase (e.g. AI_SESSION_CHAT_ORIGIN) must still match it.
    configured = dataclasses.replace(settings(tmp_path), chat_origin="https://Chat.Test")
    handle = active_session_cookie(configured)
    app = create_app(
        configured,
        clock=lambda: NOW,
        model_turn_service=ScriptedTurnService(chunks=["Hel", "lo!"]),
    )

    response = asyncio.run(_post_chat(app, cookie=handle, origin="https://chat.test"))
    assert response.status_code == 200


def test_ac1_chat_turn_accepts_a_valid_ai_session_cookie(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    app = create_app(
        configured,
        clock=lambda: NOW,
        model_turn_service=ScriptedTurnService(chunks=["Hel", "lo!"]),
    )

    response = asyncio.run(_post_chat(app, cookie=handle))

    assert response.status_code == 200
    assert response.text == "Hello!"


# --- AC2: response chunks render progressively with an understandable status -----


def test_ac2_route_streams_the_services_async_generator_without_buffering_it(
    tmp_path: Path,
) -> None:
    """The route must forward the turn service's chunks as they arrive.

    `httpx.ASGITransport` can coalesce fast in-process responses into one read, so
    this asserts the transport-independent contract instead: the route hands
    `StreamingResponse` the service's own async generator rather than collecting it
    into a string first, which is what makes chunk-by-chunk delivery possible at all.
    TICK-063 (D16) leans on exactly this: the turn's reply streams once the routing
    inference has finished.
    """
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    seen: list[str] = []

    class RecordingTurnService(ScriptedTurnService):
        async def stream_reply(
            self,
            handle: str,
            message: str,
            image_base64: str | None = None,
            access_token: str | None = None,
            patient_id: str | None = None,
        ) -> AsyncIterator[str]:
            async for chunk in super().stream_reply(
                handle, message, image_base64, access_token, patient_id
            ):
                seen.append(chunk)
                yield chunk

    app = create_app(
        configured,
        clock=lambda: NOW,
        model_turn_service=RecordingTurnService(chunks=["First chunk. ", "Second chunk."]),
    )

    response = asyncio.run(_post_chat(app, cookie=handle))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "First chunk. Second chunk."
    assert seen == ["First chunk. ", "Second chunk."]


def test_ac2_status_region_is_a_live_region_with_understandable_phases() -> None:
    assert 'id="chat-status" role="status" aria-live="polite"' in CHAT_PAGE_HTML
    scripts = re.findall(r"<script>(.*?)</script>", CHAT_PAGE_HTML, re.S)[0]
    for phase in ("Sending...", "Receiving response...", "Response complete."):
        assert phase in scripts


# --- AC3: AI-server/LLM unavailability surfaces native-scheduler instructions ----


def test_ac3_unavailable_turn_service_streams_the_fixed_fallback_text(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    app = create_app(
        configured, clock=lambda: NOW, model_turn_service=unavailable_model_turn_service()
    )

    response = asyncio.run(_post_chat(app, cookie=handle))

    assert response.status_code == 200
    assert response.text == ASSISTANT_UNAVAILABLE_RESPONSE
    # FR-19 still wants a route to OpenEMR's own scheduling UI from this path.
    assert "OpenEMR portal menu" in ASSISTANT_UNAVAILABLE_RESPONSE


def test_ticket_048_unavailable_message_is_distinct_and_not_misleading() -> None:
    """The whole-chat outage and the one-tool outage are two different failures and say
    two different things.

    TICK-048 made this point against `groq.py`'s `PLANNING_FAILED_RESPONSE`, which
    TICK-064 deleted with the planner. The distinction it was protecting survives and
    still matters: `GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE` is now the per-capability
    string, and it must not be mistakable for the deployment-wide one (AC6).
    """
    assert ASSISTANT_UNAVAILABLE_RESPONSE != GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE
    # The narrow one must not claim the assistant is down; the rest of the turn works.
    assert "not available right now" not in GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE
    assert "Everything else still works" in GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE

    # The turn may have been about anything, so this reports the *assistant* as
    # unavailable rather than claiming scheduling assistance specifically failed...
    assert "could not handle your request" in ASSISTANT_UNAVAILABLE_RESPONSE
    assert "Scheduling assistance is unavailable" not in ASSISTANT_UNAVAILABLE_RESPONSE
    # ...and it never sends a patient portal user to the staff-only native screen.
    assert "native scheduling screen" not in ASSISTANT_UNAVAILABLE_RESPONSE
    # The one scheduling mention is a conditional next step, not a diagnosis, and it
    # names the same patient-reachable place the client-side fallback panel does.
    assert "To book or change an appointment" in ASSISTANT_UNAVAILABLE_RESPONSE
    assert "OpenEMR portal menu" in CHAT_PAGE_HTML


def test_ticket_048_unconfigured_model_answers_a_non_scheduling_turn_the_same_way() -> None:
    """The reported turn -- an address change -- reaches the `client is None` call site
    directly and gets the assistant-unavailable text, not a scheduling verdict."""

    async def run() -> list[str]:
        service = unavailable_model_turn_service()
        return [
            chunk async for chunk in service.stream_reply("handle", "I need to change my address.")
        ]

    assert asyncio.run(run()) == [ASSISTANT_UNAVAILABLE_RESPONSE]


def test_ac3_page_ships_a_client_side_fallback_panel_with_openemr_instructions() -> None:
    assert 'id="chat-fallback" role="alert"' in CHAT_PAGE_HTML
    assert "OpenEMR portal menu" in CHAT_PAGE_HTML
    scripts = re.findall(r"<script>(.*?)</script>", CHAT_PAGE_HTML, re.S)[0]
    # A network failure or non-OK response (not just an in-band "unavailable" reply
    # from the model) must also surface the fallback panel.
    assert ".catch(function () {" in scripts
    assert "showFallback()" in scripts


# Seven tests stood here, all of them about `ChatService`: that `NoActionTool` never
# claimed a booking, that `_SCHEDULING_RULES` had booking disabled and reschedule and
# cancellation enabled, that `_payload()` populated `scheduling_context.open_slots` and
# `current_appointments`, and that the tool factory got the turn's credentials.
#
# TICK-064 deleted `ChatService`, `_payload()`, `SchedulingContext` and `SchedulingRules`
# outright: D13 moves scheduling planning to the local model, so no outbound payload
# carries scheduling context and there is no planner to disable a rule for. The
# capabilities those tests were really about did not go anywhere -- slot discovery,
# appointment discovery, booking and cancellation are all exercised through the tool
# surface in `test_model_turn.py`, against the same services.


def test_tick034_api_chat_route_passes_the_sessions_access_token_and_patient_id(
    tmp_path: Path,
) -> None:
    """AC1: `/api/chat` retrieves the session's access token (and the patient id
    booking also needs) via the same `SessionStore` methods TICK-035 already
    established, and makes them available to the turn service for this turn only."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    tokens = OAuthTokens(
        "real-access-token", "real-refresh-token", "nonce", patient_uuid="patient-uuid"
    )
    handle = store.create_session(tokens, NOW, configured.session_ttl)

    received: list[tuple[str | None, str | None]] = []

    @dataclass
    class RecordingTurnService:
        async def stream_reply(
            self,
            handle: str,
            message: str,
            image_base64: str | None = None,
            access_token: str | None = None,
            patient_id: str | None = None,
        ) -> AsyncIterator[str]:
            del handle, message, image_base64
            received.append((access_token, patient_id))
            yield "ok"

        def discard(self, handle: str) -> None:
            del handle

    app = create_app(configured, clock=lambda: NOW, model_turn_service=RecordingTurnService())

    response = asyncio.run(_post_chat(app, cookie=handle))

    assert response.status_code == 200
    assert received == [("real-access-token", "patient-uuid")]


# --- AC4: keyboard navigation, labels, visible focus, contrast, non-colour status -


def test_ac4_message_input_has_a_visible_programmatic_label() -> None:
    assert '<label for="chat-input">Message</label>' in CHAT_PAGE_HTML
    assert 'id="chat-input"' in CHAT_PAGE_HTML


def test_ac4_only_native_interactive_elements_are_used_so_tab_order_is_free() -> None:
    # No custom widgets (div/span with onclick or a positive tabindex) that would
    # need bespoke keyboard handling; native <textarea>/<button> are used instead.
    assert re.search(r'tabindex="[1-9]', CHAT_PAGE_HTML) is None
    assert "onclick=" not in CHAT_PAGE_HTML
    assert re.search(r"<(textarea|button)\b", CHAT_PAGE_HTML)


def test_ac4_focus_visible_outline_is_defined_and_never_suppressed() -> None:
    assert ":focus-visible {" in CHAT_PAGE_HTML
    assert "outline: 3px solid" in CHAT_PAGE_HTML
    assert "outline: none" not in CHAT_PAGE_HTML
    assert "outline:none" not in CHAT_PAGE_HTML


def test_ac4_status_and_fallback_are_not_colour_only_cues() -> None:
    # The status line is always prefixed with the word "Status:" and the fallback
    # panel always carries a text heading, independent of any colour styling.
    assert 'textContent = "Status: "' in CHAT_PAGE_HTML
    assert "<strong>Chat unavailable.</strong>" in CHAT_PAGE_HTML


def test_ac4_declared_colour_pairs_meet_wcag_aa_contrast() -> None:
    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def luminance(hex_colour: str) -> float:
        hex_colour = hex_colour.lstrip("#")
        r, g, b = (int(hex_colour[i : i + 2], 16) for i in (0, 2, 4))
        r, g, b = (linear(c) for c in (r, g, b))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(a: str, b: str) -> float:
        hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    # Text pairs actually used in CHAT_PAGE_HTML: body text, button text, and the
    # fallback panel's text, each against the background they render on.
    assert contrast("#1a1a1a", "#ffffff") >= 4.5  # body text on page background
    assert contrast("#ffffff", "#0b5fff") >= 4.5  # button label on accent fill
    assert contrast("#7a1f1a", "#fdecea") >= 4.5  # fallback text on fallback background
    assert contrast("#0b5fff", "#ffffff") >= 3.0  # focus outline against the background


def test_ac4_chat_page_is_served_without_requiring_a_session(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), clock=lambda: NOW)

    async def scenario() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://chat.test"
            ) as client:
                return await client.get("/")

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.text == CHAT_PAGE_HTML


# `test_ticket_010_privacy_rejection_still_never_calls_the_model` stood here, driving
# `ChatService` with a client that raised if called. Its property -- Presidio rejects
# and the external model is never reached -- is unchanged and now proven against the one
# path that actually egresses, in `test_general_knowledge.py`.


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_ac9_the_settings_split_did_not_disable_the_chat_origin_check(
    tmp_path: Path,
) -> None:
    """TICK-051 AC9: `POST /api/chat` still accepts the chat page's own same-origin
    fetch and still rejects everything else, now that the allowlist is its own setting.

    This is the regression the split could have caused silently. The Origin check is
    the *only* CSRF defense on this route -- the session cookie is `SameSite=None` so
    that it survives the cross-site portal iframe, which means it rides along on any
    origin's request and proves nothing about who sent it. A split that repointed the
    allowlist at the dashboard, or dropped it, would leave every test above passing.

    The dashboard's own origin is asserted rejected specifically: it is the value the
    single pre-TICK-051 setting would have taken once the destination was fixed.
    """
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    app = create_app(
        configured,
        clock=lambda: NOW,
        model_turn_service=ScriptedTurnService(chunks=["Hel", "lo!"]),
    )

    accepted = asyncio.run(_post_chat(app, cookie=handle, origin="https://chat.test"))
    assert accepted.status_code == 200

    dashboard_origin = asyncio.run(_post_chat(app, cookie=handle, origin="https://emr.test"))
    assert dashboard_origin.status_code == 403

    foreign_origin = asyncio.run(_post_chat(app, cookie=handle, origin="https://attacker.test"))
    assert foreign_origin.status_code == 403

    absent_origin = asyncio.run(_post_chat(app, cookie=handle, origin=None))
    assert absent_origin.status_code == 403
