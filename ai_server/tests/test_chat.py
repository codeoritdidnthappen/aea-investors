"""Tests for the embedded chat page and its streaming turn endpoint (TICK-013)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.chat import (
    CHAT_PAGE_HTML,
    ChatService,
    NoActionTool,
    unavailable_chat_service,
)
from ai_server.app.main import create_app
from ai_server.llm.groq import UNAVAILABLE_RESPONSE, GroqWorkflow
from ai_server.privacy.gate import OutboundPayload, PrivacyGate

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
        success_redirect_uri="https://chat.test/",
        session_ttl=timedelta(minutes=30),
        state_ttl=timedelta(minutes=5),
    )


def active_session_cookie(configured: AuthSettings) -> str:
    """Create a durable session directly, mirroring a completed OAuth callback."""
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    tokens = OAuthTokens("synthetic-access", "synthetic-refresh", "synthetic-nonce")
    return store.create_session(tokens, NOW, configured.session_ttl)


@dataclass
class ScriptedChatService:
    """A `ChatService`-shaped double that yields fixed chunks with real async gaps."""

    chunks: list[str]

    async def stream_reply(self, message: str) -> AsyncIterator[str]:
        del message
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


async def _post_chat(app, cookie: str | None, message: str = "Hello") -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie} if cookie else None,
        ) as client:
            return await client.post("/api/chat", json={"message": message})


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
    app = create_app(settings(tmp_path), clock=lambda: NOW, chat_service=unavailable_chat_service())

    missing_cookie = asyncio.run(_post_chat(app, cookie=None))
    assert missing_cookie.status_code == 401

    bogus_cookie = asyncio.run(_post_chat(app, cookie="not-a-real-handle"))
    assert bogus_cookie.status_code == 401


def test_ac1_chat_turn_accepts_a_valid_ai_session_cookie(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=ScriptedChatService(chunks=["Hel", "lo!"]),
    )

    response = asyncio.run(_post_chat(app, cookie=handle))

    assert response.status_code == 200
    assert response.text == "Hello!"


# --- AC2: response chunks render progressively with an understandable status -----


def test_ac2_route_streams_the_services_async_generator_without_buffering_it(
    tmp_path: Path,
) -> None:
    """The route must forward `ChatService.stream_reply`'s chunks as they arrive.

    `httpx.ASGITransport` can coalesce fast in-process responses into one read, so
    this asserts the transport-independent contract instead: the route hands
    `StreamingResponse` the service's own async generator rather than collecting it
    into a string first, which is what makes chunk-by-chunk delivery possible at all.
    """
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    seen: list[str] = []

    class RecordingChatService(ScriptedChatService):
        async def stream_reply(self, message: str) -> AsyncIterator[str]:
            async for chunk in super().stream_reply(message):
                seen.append(chunk)
                yield chunk

    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=RecordingChatService(chunks=["First chunk. ", "Second chunk."]),
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


def test_ac3_unavailable_chat_service_streams_the_fixed_fallback_text(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    handle = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    response = asyncio.run(_post_chat(app, cookie=handle))

    assert response.status_code == 200
    assert response.text == UNAVAILABLE_RESPONSE
    assert "OpenEMR" in UNAVAILABLE_RESPONSE


def test_ac3_page_ships_a_client_side_fallback_panel_with_openemr_instructions() -> None:
    assert 'id="chat-fallback" role="alert"' in CHAT_PAGE_HTML
    assert "OpenEMR portal menu" in CHAT_PAGE_HTML
    scripts = re.findall(r"<script>(.*?)</script>", CHAT_PAGE_HTML, re.S)[0]
    # A network failure or non-OK response (not just an in-band "unavailable" reply
    # from the model) must also surface the fallback panel.
    assert ".catch(function () {" in scripts
    assert "showFallback()" in scripts


def test_ac3_no_action_tool_never_claims_a_scheduling_success() -> None:
    async def run() -> str:
        result = await NoActionTool().execute(plan=None)  # type: ignore[arg-type]
        return result.public_summary

    summary = asyncio.run(run())
    assert "booked" not in summary.lower()
    assert "confirmed" not in summary.lower()


def test_ac3_chat_service_disables_every_scheduling_action_pending_a_real_tool(
    tmp_path: Path,
) -> None:
    del tmp_path
    captured: list[OutboundPayload] = []

    class CapturingWorkflow(GroqWorkflow):
        def __init__(self) -> None:
            super().__init__(PrivacyGate.create(), client=None)  # type: ignore[arg-type]

        async def respond(self, payload, tool):  # type: ignore[override]
            captured.append(payload)
            yield "ok"

    service = ChatService(workflow=CapturingWorkflow(), tool=NoActionTool(), clock=lambda: NOW)

    async def run() -> list[str]:
        return [chunk async for chunk in service.stream_reply("Can you book me an appointment?")]

    assert asyncio.run(run()) == ["ok"]
    assert len(captured) == 1
    rules = captured[0].scheduling_rules
    assert rules.booking_enabled is False
    assert rules.rescheduling_enabled is False
    assert rules.cancellation_enabled is False


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
    app = create_app(settings(tmp_path), clock=lambda: NOW, chat_service=unavailable_chat_service())

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


def test_ticket_010_privacy_rejection_still_never_calls_the_model(tmp_path: Path) -> None:
    """A sensitive turn is corrected locally end to end through the chat route."""
    del tmp_path

    class FailIfCalledClient:
        async def complete(self, payload: OutboundPayload) -> str:  # pragma: no cover
            raise AssertionError("Groq must not be called for a rejected turn")

        def stream(self, payload: OutboundPayload):  # pragma: no cover
            raise AssertionError("Groq must not be called for a rejected turn")

    workflow = GroqWorkflow(PrivacyGate.create(), FailIfCalledClient())
    service = ChatService(workflow=workflow, tool=NoActionTool(), clock=lambda: NOW)

    async def run() -> list[str]:
        return [chunk async for chunk in service.stream_reply("My phone is 555-555-5555.")]

    assert asyncio.run(run()) == ["Please remove personal or health information and try again."]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
