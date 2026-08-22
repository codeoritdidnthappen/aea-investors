"""Tests for ending an AI session, and for sweeping the ones nobody comes back to.

TICK-055: before this, the AI server had no logout at all. OpenEMR's portal sign-out
ends its own PHP session but cannot touch the `ai_session` cookie on the chat origin,
so the session row -- holding the patient's AES-GCM-encrypted OpenEMR access and
refresh tokens -- stayed valid for the rest of its 8-hour TTL, and navigating straight
to the chat origin resumed chatting as that patient (FR-1, NFR-31).
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.chat import unavailable_chat_service
from ai_server.app.main import create_app

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTROLLER = (
    _REPO_ROOT
    / "openemr_modules"
    / "aeai-portal-chat"
    / "src"
    / "Controller"
    / "PortalChatController.php"
)


def settings(tmp_path: Path, portal_origin: str | None = "https://emr.test") -> AuthSettings:
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
        portal_origin=portal_origin,
        session_ttl=timedelta(minutes=30),
        state_ttl=timedelta(minutes=5),
    )


def open_store(configured: AuthSettings) -> SessionStore:
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    return store


def active_session_cookie(configured: AuthSettings, now: datetime = NOW) -> str:
    """Create a durable session directly, mirroring a completed OAuth callback."""
    tokens = OAuthTokens("synthetic-access", "synthetic-refresh", "synthetic-nonce")
    return open_store(configured).create_session(tokens, now, configured.session_ttl)


def session_row_count(database_path: Path) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    finally:
        connection.close()


async def _post(
    app,
    path: str,
    cookie: str | None,
    origin: str | None = "https://chat.test",
    json_body: dict[str, object] | None = None,
) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie} if cookie else None,
        ) as client:
            return await client.post(
                path,
                json=json_body,
                headers={"origin": origin} if origin else None,
            )


# --- AC1: a logout path that deletes the row and clears the cookie ----------------


def test_ac1_logout_deletes_the_session_row_rather_than_expiring_it(tmp_path: Path) -> None:
    """The row carries the encrypted access and refresh tokens. Marking it expired
    would leave both on disk; only deleting it ends the patient's exposure."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    assert session_row_count(configured.database_path) == 1
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    response = asyncio.run(_post(app, "/api/logout", cookie=cookie))

    assert response.status_code == 204
    assert session_row_count(configured.database_path) == 0


def test_ac1_logout_clears_the_cookie_with_every_attribute_it_was_set_with(
    tmp_path: Path,
) -> None:
    """A Set-Cookie that differs in path/SameSite/Secure is a *different* cookie: the
    browser stores the clear alongside the original and the session cookie survives.
    Starlette's delete_cookie defaults to SameSite=Lax, which Chrome also drops
    outright in the cross-site context the portal sign-out hook creates."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    response = asyncio.run(_post(app, "/api/logout", cookie=cookie))

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith('ai_session=""') or set_cookie.startswith("ai_session=;")
    assert "Max-Age=0" in set_cookie
    assert "Path=/" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=none" in set_cookie.lower()


def test_ac1_logout_is_idempotent_and_reveals_nothing_about_the_handle(
    tmp_path: Path,
) -> None:
    """Logging out twice, or presenting a handle that was never a session, must look
    identical -- otherwise the response is an oracle for guessing live handles."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    first = asyncio.run(_post(app, "/api/logout", cookie=cookie))
    second = asyncio.run(_post(app, "/api/logout", cookie=cookie))
    never_existed = asyncio.run(_post(app, "/api/logout", cookie="not-a-real-handle"))
    no_cookie_at_all = asyncio.run(_post(app, "/api/logout", cookie=None))

    assert first.status_code == 204
    assert second.status_code == 204
    assert never_existed.status_code == 204
    assert no_cookie_at_all.status_code == 204


def test_ac1_logout_drops_in_memory_patient_answers_with_the_row(tmp_path: Path) -> None:
    """Onboarding and the address flow both hold the patient's own half-finished
    answers in this process's memory, keyed by the handle. Deleting the encrypted
    tokens while leaving a name and date of birth in a dict would not be a logout."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    discarded: list[str] = []

    class RecordingService:
        def discard(self, handle: str) -> None:
            discarded.append(handle)

    recording = RecordingService()
    app = create_app(
        configured,
        clock=lambda: NOW,
        chat_service=unavailable_chat_service(),
        onboarding_service=recording,  # type: ignore[arg-type]
        address_service=recording,  # type: ignore[arg-type]
    )

    response = asyncio.run(_post(app, "/api/logout", cookie=cookie))

    assert response.status_code == 204
    assert discarded == [cookie, cookie]


# --- AC3: the previous patient's session cannot be resumed afterwards --------------


def test_ac3_chat_does_not_resume_the_session_after_logout(tmp_path: Path) -> None:
    """The whole point (FR-1): the same cookie that worked a moment ago must stop
    working, without the browser having to cooperate in clearing it."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    before = asyncio.run(_post(app, "/api/chat", cookie=cookie, json_body={"message": "Hello"}))
    assert before.status_code == 200

    assert asyncio.run(_post(app, "/api/logout", cookie=cookie)).status_code == 204

    after = asyncio.run(_post(app, "/api/chat", cookie=cookie, json_body={"message": "Hello"}))
    assert after.status_code == 401


# --- AC4: logout is not a CSRF sink -----------------------------------------------


def test_ac4_an_off_origin_request_cannot_log_a_patient_out(tmp_path: Path) -> None:
    """`ai_session` is SameSite=None so it rides along on any origin's request. The
    Origin check is the actual defense, exactly as it is for /api/chat."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    forged = asyncio.run(_post(app, "/api/logout", cookie=cookie, origin="https://evil.test"))
    assert forged.status_code == 403

    missing = asyncio.run(_post(app, "/api/logout", cookie=cookie, origin=None))
    assert missing.status_code == 403

    # Refused, not merely reported as refused.
    assert session_row_count(configured.database_path) == 1


def test_ac4_logout_is_not_reachable_by_a_get(tmp_path: Path) -> None:
    """A GET carries no Origin a browser will not let a page forge, so a
    state-changing GET would be reachable from any off-origin <img> or redirect."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    async def scenario() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://chat.test",
                cookies={"ai_session": cookie},
            ) as client:
                return await client.get("/api/logout")

    assert asyncio.run(scenario()).status_code == 405
    assert session_row_count(configured.database_path) == 1


def test_ac4_the_portal_origin_is_accepted_only_when_configured(tmp_path: Path) -> None:
    """The portal's own origin is the one extra origin logout takes, and only because
    the portal session cookie is SameSite=Strict so sign-out cannot be routed through
    the chat origin. A deployment that does not configure one gets no exception."""
    configured = settings(tmp_path, portal_origin="https://emr.test")
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    accepted = asyncio.run(_post(app, "/api/logout", cookie=cookie, origin="https://emr.test"))
    assert accepted.status_code == 204
    assert session_row_count(configured.database_path) == 0

    unconfigured = settings(tmp_path / "other", portal_origin=None)
    other_cookie = active_session_cookie(unconfigured)
    unconfigured_app = create_app(
        unconfigured, clock=lambda: NOW, chat_service=unavailable_chat_service()
    )

    refused = asyncio.run(
        _post(unconfigured_app, "/api/logout", cookie=other_cookie, origin="https://emr.test")
    )
    assert refused.status_code == 403
    assert session_row_count(unconfigured.database_path) == 1


def test_ac4_the_portal_origin_is_never_accepted_by_a_chat_turn(tmp_path: Path) -> None:
    """Widening logout must not widen the chat turn: /api/chat stays chat-origin-only,
    so an embedding page still cannot drive a patient's conversation."""
    configured = settings(tmp_path, portal_origin="https://emr.test")
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    response = asyncio.run(
        _post(
            app,
            "/api/chat",
            cookie=cookie,
            origin="https://emr.test",
            json_body={"message": "Hello"},
        )
    )

    assert response.status_code == 403


# --- AC2: portal sign-out reaches that endpoint -----------------------------------


def test_ac2_module_ends_the_ai_session_on_a_portal_sign_out_click() -> None:
    controller = _CONTROLLER.read_text(encoding="utf-8")

    # Both portal sign-out entries -- the dashboard tile (`./logout.php`) and the nav
    # menu item (`logout.php`) -- render as anchors whose href ends in logout.php.
    assert "a[href$=\\'logout.php\\']" in controller
    assert "navigator.sendBeacon(endpoint)" in controller
    # sendBeacon is the only call specified to survive the unload that immediately
    # follows the click; a fetch/XHR is cancelled with the document. It is also not
    # `fetch(`/`XMLHttpRequest`, which the module is separately forbidden to contain.
    for banned in ("fetch(", "XMLHttpRequest"):
        assert banned not in controller


def test_ac2_module_logout_endpoint_is_configurable_with_a_local_default() -> None:
    controller = _CONTROLLER.read_text(encoding="utf-8")

    assert "getenv('AEAI_PORTAL_CHAT_LOGOUT_URL')" in controller
    assert "https://chat.localhost/api/logout" in controller
    # Escaped the same way the launch URL is; it is the only value this script puts in
    # the DOM, and no token or patient identifier goes near it (FR-4).
    assert "htmlspecialchars($logoutUrl, ENT_QUOTES, 'UTF-8')" in controller


def test_ac2_deployment_wires_the_logout_endpoint_and_the_allowed_portal_origin() -> None:
    compose = (_REPO_ROOT / "deploy" / "local" / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (_REPO_ROOT / "deploy" / "local" / ".env.example").read_text(encoding="utf-8")

    # A hook pointed at nothing, or a server that refuses the portal's origin, is the
    # silent no-op TICK-055 AC2 calls a failure -- so both halves ship configured.
    assert (
        "AEAI_PORTAL_CHAT_LOGOUT_URL: ${AEAI_PORTAL_CHAT_LOGOUT_URL:-https://chat.localhost"
        "/api/logout}" in compose
    )
    assert "AI_SESSION_PORTAL_ORIGIN: ${AI_SESSION_PORTAL_ORIGIN:-https://emr.localhost}" in compose
    assert "AEAI_PORTAL_CHAT_LOGOUT_URL=https://chat.localhost/api/logout" in env_example
    assert "AI_SESSION_PORTAL_ORIGIN=https://emr.localhost" in env_example


# --- AC5: expired sessions are swept without their handle being presented ----------


def test_ac5_an_abandoned_expired_session_is_purged_at_startup(tmp_path: Path) -> None:
    """The `ai_session` cookie has no max_age, so closing the browser discards it and
    the handle is never presented again. `active_session`'s lazy delete therefore never
    fires and the row -- with its encrypted tokens -- was retained indefinitely.

    Nothing in this test reads or presents the handle after creating it.
    """
    configured = settings(tmp_path)
    active_session_cookie(configured, now=NOW - timedelta(hours=9))
    assert session_row_count(configured.database_path) == 1
    after_expiry = NOW

    app = create_app(
        configured, clock=lambda: after_expiry, chat_service=unavailable_chat_service()
    )

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(scenario())

    assert session_row_count(configured.database_path) == 0


def test_ac5_a_still_active_session_survives_the_sweep(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(scenario())

    assert session_row_count(configured.database_path) == 1


def test_ac5_the_sweep_keeps_running_after_startup(tmp_path: Path) -> None:
    """Startup alone does not bound retention for a container that runs for weeks: a
    session created after boot and then abandoned must still be collected."""
    configured = dataclasses.replace(settings(tmp_path), sweep_interval=timedelta(milliseconds=10))
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    async def scenario() -> int:
        async with app.router.lifespan_context(app):
            # Created *after* the startup purge, already expired, never presented.
            active_session_cookie(configured, now=NOW - timedelta(hours=9))
            assert session_row_count(configured.database_path) == 1
            for _ in range(200):
                await asyncio.sleep(0.01)
                if session_row_count(configured.database_path) == 0:
                    break
            return session_row_count(configured.database_path)

    assert asyncio.run(scenario()) == 0


# --- AC6: the absolute TTL stays absolute, and is announced before it lands ---------


def test_ac6_a_session_near_its_hard_cut_warns_the_patient_in_the_reply(
    tmp_path: Path,
) -> None:
    """The TTL is stamped once by create_session and never extended -- the stored
    refresh token is never redeemed (evidence/TICK-055/DECISIONS.md). A patient
    mid-conversation is therefore cut off at a fixed instant, so the cut is announced
    rather than sprung."""
    configured = settings(tmp_path)  # session_ttl = 30 minutes
    cookie = active_session_cookie(configured)
    # 5 minutes left, inside the default 30-minute warning window.
    near_expiry = NOW + timedelta(minutes=25)
    app = create_app(configured, clock=lambda: near_expiry, chat_service=unavailable_chat_service())

    response = asyncio.run(_post(app, "/api/chat", cookie=cookie, json_body={"message": "Hello"}))

    assert response.status_code == 200
    assert "this chat session ends in about 5 minutes" in response.text
    assert "sign in from your patient portal again" in response.text


def test_ac6_a_session_with_time_left_is_not_warned(tmp_path: Path) -> None:
    # The real 8-hour TTL, so a freshly signed-in patient is well outside the
    # 30-minute warning window and the notice does not ride on every turn.
    configured = dataclasses.replace(settings(tmp_path), session_ttl=timedelta(hours=8))
    cookie = active_session_cookie(configured)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    response = asyncio.run(_post(app, "/api/chat", cookie=cookie, json_body={"message": "Hello"}))

    assert response.status_code == 200
    assert "this chat session ends" not in response.text


def test_ac6_the_ttl_is_never_extended_by_activity(tmp_path: Path) -> None:
    """Recorded as a decision, and pinned here so it cannot drift silently: nothing in
    a chat turn moves expires_at. If a future ticket redeems the refresh token to
    renew a session, this test is the one that should be changed deliberately."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured)
    store = open_store(configured)
    expiry_at_creation = store.expires_at(cookie, NOW)
    app = create_app(configured, clock=lambda: NOW, chat_service=unavailable_chat_service())

    asyncio.run(_post(app, "/api/chat", cookie=cookie, json_body={"message": "Hello"}))

    assert store.expires_at(cookie, NOW) == expiry_at_creation
    assert expiry_at_creation == NOW + configured.session_ttl


# --- store-level behaviour --------------------------------------------------------


def test_delete_session_removes_an_already_expired_row_too(tmp_path: Path) -> None:
    """Logout takes no `now`: there is no clock at which a logged-out session's
    encrypted tokens should stay on disk, expired-but-unswept included."""
    configured = settings(tmp_path)
    cookie = active_session_cookie(configured, now=NOW - timedelta(hours=9))
    store = open_store(configured)

    assert store.delete_session(cookie) is True
    assert store.delete_session(cookie) is False
    assert session_row_count(configured.database_path) == 0


def test_expires_at_is_none_for_an_absent_or_expired_session(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = open_store(configured)
    expired = active_session_cookie(configured, now=NOW - timedelta(hours=9))

    assert store.expires_at("never-a-session", NOW) is None
    assert store.expires_at(expired, NOW) is None
