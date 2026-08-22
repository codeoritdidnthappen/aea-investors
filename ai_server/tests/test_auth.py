"""Synthetic integration tests for the OAuth callback and durable session store."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI

from ai_server.app.auth import (
    AuthError,
    AuthorizationService,
    AuthSettings,
    OAuthTokens,
    OpenEmrOAuthClient,
    SessionStore,
    utc_now,
)
from ai_server.app.chat import CHAT_PAGE_HTML
from ai_server.app.main import create_app

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


class SyntheticOAuthClient:
    """A deterministic token endpoint used only for callback integration tests."""

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        self.verifiers: list[str] = []

    async def exchange(self, code: str, verifier: str) -> OAuthTokens:
        assert code == "synthetic-code"
        self.verifiers.append(verifier)
        return OAuthTokens("synthetic-access-token", "synthetic-refresh-token", self.nonce)


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
    )


def database_row(database_path: Path, query: str) -> tuple[object, ...] | None:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(query).fetchone()
    finally:
        connection.close()


def test_ac1_environment_settings_require_a_32_byte_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment = {
        "AI_SESSION_DATABASE_PATH": str(tmp_path / "sessions.sqlite3"),
        "AI_SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        "OPENEMR_OAUTH_AUTHORIZE_URL": "https://openemr.test/authorize",
        "OPENEMR_OAUTH_TOKEN_URL": "https://openemr.test/token",
        "OPENEMR_OAUTH_JWKS_URL": "https://openemr.test/jwks",
        "OPENEMR_OAUTH_ISSUER": "https://openemr.test",
        "OPENEMR_OAUTH_CLIENT_ID": "synthetic-client",
        "OPENEMR_OAUTH_CLIENT_SECRET": "synthetic-secret",
        "OPENEMR_OAUTH_REDIRECT_URI": "https://chat.test/oauth/callback",
        "AI_SESSION_DASHBOARD_REDIRECT_URI": "https://emr.test/portal/home.php",
        "AI_SESSION_CHAT_ORIGIN": "https://chat.test",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert AuthSettings.from_environment().encryption_key == b"k" * 32


def _complete_environment(tmp_path: Path) -> dict[str, str]:
    """Every variable `AuthSettings.from_environment()` requires, all valid."""
    return {
        "AI_SESSION_DATABASE_PATH": str(tmp_path / "sessions.sqlite3"),
        "AI_SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        "OPENEMR_OAUTH_AUTHORIZE_URL": "https://openemr.test/authorize",
        "OPENEMR_OAUTH_TOKEN_URL": "https://openemr.test/token",
        "OPENEMR_OAUTH_JWKS_URL": "https://openemr.test/jwks",
        "OPENEMR_OAUTH_ISSUER": "https://openemr.test",
        "OPENEMR_OAUTH_CLIENT_ID": "synthetic-client",
        "OPENEMR_OAUTH_CLIENT_SECRET": "synthetic-secret",
        "OPENEMR_OAUTH_REDIRECT_URI": "https://chat.test/oauth/callback",
        "AI_SESSION_DASHBOARD_REDIRECT_URI": "https://emr.test/portal/home.php",
        "AI_SESSION_CHAT_ORIGIN": "https://chat.test",
    }


# --- TICK-051 AC1: the destination and the chat-origin allowlist are two settings ---


@pytest.mark.parametrize(
    "variable",
    ["AI_SESSION_DASHBOARD_REDIRECT_URI", "AI_SESSION_CHAT_ORIGIN"],
)
def test_ac1_both_split_settings_must_be_absolute_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    """TICK-051 AC1: the boot-time absolute-URL check applies to *both* halves of the
    split, not only to the one that inherited the old code path.

    A relative dashboard URL sends the patient to a path on the chat host instead of
    the portal; a relative chat origin never matches a browser `Origin` header and 403s
    every chat turn forever. Both have to fail here, at boot, naming the variable.
    """
    environment = _complete_environment(tmp_path)
    environment[variable] = "/"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=f"{variable} must be an absolute URL"):
        AuthSettings.from_environment()


@pytest.mark.parametrize(
    "variable",
    ["AI_SESSION_DASHBOARD_REDIRECT_URI", "AI_SESSION_CHAT_ORIGIN"],
)
def test_ac1_neither_split_setting_can_be_changed_by_editing_the_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    """TICK-051 AC1: neither setting is derived from the other, so editing one leaves
    the other exactly where it was.

    This is the whole point of the split. While `POST /api/chat`'s Origin allowlist was
    derived from the post-login redirect target, repointing the destination at the
    dashboard -- which FR-31 requires -- would silently have made `emr.localhost` the
    only origin allowed to call the chat API, 403-ing the chat page's own fetch on
    every turn.
    """
    environment = _complete_environment(tmp_path)
    environment[variable] = "https://moved.test/somewhere"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    configured = AuthSettings.from_environment()

    unchanged = (
        configured.chat_origin
        if variable == "AI_SESSION_DASHBOARD_REDIRECT_URI"
        else configured.dashboard_redirect_uri
    )
    assert (
        unchanged
        == _complete_environment(tmp_path)[
            "AI_SESSION_CHAT_ORIGIN"
            if variable == "AI_SESSION_DASHBOARD_REDIRECT_URI"
            else "AI_SESSION_DASHBOARD_REDIRECT_URI"
        ]
    )


def test_ac10_the_renamed_redirect_setting_fails_boot_rather_than_being_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TICK-051 AC10: the redirect setting was renamed, not re-meaned.

    The failure this guards is specific: a deployment that adds the new chat-origin
    variable but keeps only the old `AI_SESSION_SUCCESS_REDIRECT_URI` -- still pointed
    at the chat page. `from_environment` only fails on *missing* variables, so had the
    old name been kept and quietly given a new meaning, that deployment would boot
    cleanly with the patient still landing on the full-page chat and nothing anywhere
    reporting it. Under the new name it cannot start at all.
    """
    environment = _complete_environment(tmp_path)
    del environment["AI_SESSION_DASHBOARD_REDIRECT_URI"]
    environment["AI_SESSION_SUCCESS_REDIRECT_URI"] = "https://chat.test/"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AI_SESSION_DASHBOARD_REDIRECT_URI", raising=False)

    with pytest.raises(RuntimeError, match="AI_SESSION_DASHBOARD_REDIRECT_URI"):
        AuthSettings.from_environment()


def signed_id_token(
    settings: AuthSettings,
    nonce: str,
    expires_at: int,
    extra_claims: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "kid": "test-key"}).encode()
    ).rstrip(b"=")
    claims = base64.urlsafe_b64encode(
        json.dumps(
            {
                "iss": settings.issuer,
                "aud": settings.client_id,
                "exp": expires_at,
                "nonce": nonce,
                **(extra_claims or {}),
            }
        ).encode()
    ).rstrip(b"=")
    signed_data = b".".join((header, claims))
    signature = private_key.sign(signed_data, padding.PKCS1v15(), hashes.SHA256())
    modulus = (
        base64.urlsafe_b64encode(
            public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    exponent = (
        base64.urlsafe_b64encode(
            public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return (
        f"{signed_data.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}",
        {"keys": [{"kty": "RSA", "kid": "test-key", "n": modulus, "e": exponent}]},
    )


def test_ac1_code_exchange_validates_signed_id_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        id_token, jwks = signed_id_token(configured, "expected", int(utc_now().timestamp()) + 60)

        async def token_response(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(configured.jwks_url):
                return httpx.Response(200, json=jwks)
            assert request.url == httpx.URL(configured.token_url)
            assert b"code_verifier=verifier" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "refresh_token": "synthetic-refresh-token",
                    "id_token": id_token,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(token_response)) as client:
            tokens = await OpenEmrOAuthClient(configured, client).exchange("code", "verifier")
        assert tokens.id_token_nonce == "expected"
        # No fhirUser/sub claim on this token -- TICK-028 found this OpenEMR version
        # carries the bound patient id there, not a top-level `patient` field; its
        # absence must not fail an otherwise valid exchange (TICK-035).
        assert tokens.patient_uuid is None

    asyncio.run(scenario())


def test_ac1_code_exchange_extracts_patient_uuid_from_the_fhir_user_claim(
    tmp_path: Path,
) -> None:
    """TICK-035: `evidence/TICK-028/BINDING_MATRIX.md` proved the bound patient is
    confirmed via the ID token's `fhirUser`/`sub` claims; `fhirUser` (a
    `Patient/<uuid>` reference) is preferred when present."""

    async def scenario() -> None:
        configured = settings(tmp_path)
        id_token, jwks = signed_id_token(
            configured,
            "expected",
            int(utc_now().timestamp()) + 60,
            extra_claims={"fhirUser": "Patient/synthetic-patient-uuid", "sub": "synthetic-sub"},
        )

        async def token_response(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(configured.jwks_url):
                return httpx.Response(200, json=jwks)
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "refresh_token": "synthetic-refresh-token",
                    "id_token": id_token,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(token_response)) as client:
            tokens = await OpenEmrOAuthClient(configured, client).exchange("code", "verifier")
        assert tokens.patient_uuid == "synthetic-patient-uuid"

    asyncio.run(scenario())


def test_ac1_code_exchange_falls_back_to_the_sub_claim_for_patient_uuid(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        id_token, jwks = signed_id_token(
            configured,
            "expected",
            int(utc_now().timestamp()) + 60,
            extra_claims={"sub": "synthetic-sub-patient-uuid"},
        )

        async def token_response(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(configured.jwks_url):
                return httpx.Response(200, json=jwks)
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "refresh_token": "synthetic-refresh-token",
                    "id_token": id_token,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(token_response)) as client:
            tokens = await OpenEmrOAuthClient(configured, client).exchange("code", "verifier")
        assert tokens.patient_uuid == "synthetic-sub-patient-uuid"

    asyncio.run(scenario())


def test_ac1_forged_or_expired_id_token_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        token, jwks = signed_id_token(configured, "expected", int(utc_now().timestamp()) + 60)
        header, claims, signature = token.split(".")
        forged_signature = f"{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
        forged_token = f"{header}.{claims}.{forged_signature}"

        async def token_response(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(configured.jwks_url):
                return httpx.Response(200, json=jwks)
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "refresh_token": "synthetic-refresh-token",
                    "id_token": forged_token,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(token_response)) as client:
            try:
                await OpenEmrOAuthClient(configured, client).exchange("code", "verifier")
            except AuthError as exc:
                assert exc.status_code == 401
            else:
                raise AssertionError("forged ID token was accepted")

    asyncio.run(scenario())


async def request(
    app: FastAPI,
    path: str,
    sec_fetch_dest: str | None = "document",
    cookie: str | None = None,
) -> httpx.Response:
    """GET `path`, declaring the position the browser would declare.

    `sec_fetch_dest` defaults to `document` because that is what a real top-level
    navigation sends and what every pre-TICK-051 caller of this helper was implicitly
    exercising; pass `None` to omit the header entirely.
    """
    headers = {} if sec_fetch_dest is None else {"sec-fetch-dest": sec_fetch_dest}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            follow_redirects=False,
            cookies={"ai_session": cookie} if cookie else None,
        ) as client:
            return await client.get(path, headers=headers)


def test_ac1_launch_uses_pkce_state_nonce_and_proven_scopes(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        store = SessionStore(configured.database_path, configured.encryption_key)
        store.initialize()
        oauth = SyntheticOAuthClient("unused")
        app = create_app(configured, AuthorizationService(configured, store, oauth), lambda: NOW)
        response = await request(app, "/oauth/launch")
        query = parse_qs(urlparse(response.headers["location"]).query)
        assert response.status_code == 302
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["state"][0]) >= 32
        assert len(query["nonce"][0]) >= 32
        assert set(query["scope"][0].split()) == set(configured.scopes)

    asyncio.run(scenario())


def test_ac2_replay_and_stale_state_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        store = SessionStore(configured.database_path, configured.encryption_key)
        store.initialize()
        oauth = SyntheticOAuthClient("placeholder")
        service = AuthorizationService(configured, store, oauth)
        location = await service.launch_url(NOW)
        state = parse_qs(urlparse(location).query)["state"][0]
        nonce = parse_qs(urlparse(location).query)["nonce"][0]
        oauth.nonce = nonce
        await service.callback("synthetic-code", state, NOW)
        try:
            await service.callback("synthetic-code", state, NOW)
        except AuthError as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("replayed state was accepted")
        stale_location = await service.launch_url(NOW)
        stale_state = parse_qs(urlparse(stale_location).query)["state"][0]
        try:
            await service.callback("synthetic-code", stale_state, NOW + timedelta(minutes=6))
        except AuthError as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("stale state was accepted")

    asyncio.run(scenario())


def test_ac2_nonce_mismatch_fails_without_creating_a_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        store = SessionStore(configured.database_path, configured.encryption_key)
        store.initialize()
        service = AuthorizationService(configured, store, SyntheticOAuthClient("wrong-nonce"))
        location = await service.launch_url(NOW)
        state = parse_qs(urlparse(location).query)["state"][0]
        try:
            await service.callback("synthetic-code", state, NOW)
        except AuthError as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("nonce mismatch was accepted")
        assert database_row(configured.database_path, "SELECT count(*) FROM sessions") == (0,)

    asyncio.run(scenario())


def test_ac3_callback_sets_only_secure_httponly_ai_session_cookie(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = settings(tmp_path)
        store = SessionStore(configured.database_path, configured.encryption_key)
        store.initialize()
        oauth = SyntheticOAuthClient("placeholder")
        service = AuthorizationService(configured, store, oauth)
        app = create_app(configured, service, lambda: NOW)
        launch = await request(app, "/oauth/launch")
        query = parse_qs(urlparse(launch.headers["location"]).query)
        oauth.nonce = query["nonce"][0]
        callback = await request(
            app, f"/oauth/callback?code=synthetic-code&state={query['state'][0]}"
        )
        cookie = callback.headers["set-cookie"].lower()
        assert callback.status_code == 303
        # TICK-051: the destination is the portal dashboard, not the chat page.
        assert callback.headers["location"] == configured.dashboard_redirect_uri
        assert "httponly" in cookie
        assert "secure" in cookie
        # The chat page is embedded as a cross-site iframe (TICK-012): samesite=lax
        # would silently withhold this cookie from the iframe's own fetch call.
        assert "samesite=none" in cookie
        assert "synthetic-access-token" not in cookie
        assert "synthetic-refresh-token" not in cookie

    asyncio.run(scenario())


def test_ac4_encrypted_session_survives_restart_without_plaintext_tokens(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    first_store = SessionStore(configured.database_path, configured.encryption_key)
    first_store.initialize()
    handle = first_store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(minutes=30),
    )
    row = database_row(
        configured.database_path,
        "SELECT handle_hash, cursor, access_nonce, access_ciphertext, "
        "refresh_nonce, refresh_ciphertext FROM sessions",
    )
    assert row is not None
    assert row[2] != row[4]
    assert b"synthetic-access-token" not in row[3]
    assert b"synthetic-refresh-token" not in row[5]
    restarted_store = SessionStore(configured.database_path, configured.encryption_key)
    restarted_store.initialize()
    assert restarted_store.active_session(handle, NOW + timedelta(minutes=1))


def test_ac5_expiry_deletes_session_and_encrypted_tokens(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(seconds=1),
    )
    assert not store.active_session(handle, NOW + timedelta(seconds=2))
    assert database_row(configured.database_path, "SELECT count(*) FROM sessions") == (0,)


def test_save_and_load_cursor_round_trips_the_onboarding_workflow_position(
    tmp_path: Path,
) -> None:
    """TICK-017 AC2: the non-patient onboarding workflow cursor persists in the same
    AI-server SQLite session store the OAuth session already lives in
    (ARCHITECTURE.md Sec. 5), so a draft can be reloaded after a restart."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(minutes=30),
    )

    assert store.load_cursor(handle, NOW) is None

    store.save_cursor(handle, "draft-1", NOW)

    assert store.load_cursor(handle, NOW) == "draft-1"


def test_load_cursor_survives_a_restart(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    first_store = SessionStore(configured.database_path, configured.encryption_key)
    first_store.initialize()
    handle = first_store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(minutes=30),
    )
    first_store.save_cursor(handle, "draft-1", NOW)

    restarted_store = SessionStore(configured.database_path, configured.encryption_key)
    restarted_store.initialize()

    assert restarted_store.load_cursor(handle, NOW + timedelta(minutes=1)) == "draft-1"


def test_load_cursor_returns_none_for_an_unknown_or_expired_session(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()

    assert store.load_cursor("unknown-handle", NOW) is None

    handle = store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(seconds=1),
    )
    store.save_cursor(handle, "draft-1", NOW)

    assert store.load_cursor(handle, NOW + timedelta(seconds=2)) is None


def test_save_cursor_is_a_no_op_for_an_unknown_or_expired_session(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()

    store.save_cursor("unknown-handle", "draft-1", NOW)

    assert database_row(configured.database_path, "SELECT count(*) FROM sessions") == (0,)


def test_access_token_round_trips_and_respects_expiry(tmp_path: Path) -> None:
    """TICK-035: `OnboardingFlow` needs the caller's delegated access token per call;
    this is the only place it's ever decrypted back to plaintext."""
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    handle = store.create_session(
        OAuthTokens("synthetic-access-token", "synthetic-refresh-token", "nonce"),
        NOW,
        timedelta(minutes=30),
    )

    assert store.access_token(handle, NOW) == "synthetic-access-token"
    assert store.access_token("unknown-handle", NOW) is None
    assert store.access_token(handle, NOW + timedelta(hours=1)) is None


def test_patient_uuid_round_trips_and_defaults_to_none_when_never_captured(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    bound_handle = store.create_session(
        OAuthTokens("access", "refresh", "nonce", patient_uuid="synthetic-patient-uuid"),
        NOW,
        timedelta(minutes=30),
    )
    unbound_handle = store.create_session(
        OAuthTokens("access", "refresh", "nonce"),
        NOW,
        timedelta(minutes=30),
    )

    assert store.patient_uuid(bound_handle, NOW) == "synthetic-patient-uuid"
    assert store.patient_uuid(unbound_handle, NOW) is None
    assert store.patient_uuid("unknown-handle", NOW) is None
    assert store.patient_uuid(bound_handle, NOW + timedelta(hours=1)) is None


def test_initialize_migrates_a_sessions_table_predating_patient_columns(
    tmp_path: Path,
) -> None:
    """Confirmed live: CREATE TABLE IF NOT EXISTS is a no-op against a `sessions`
    table that already exists from before patient_nonce/patient_ciphertext were
    added, so create_session()'s 9-value INSERT crashed with sqlite3.OperationalError
    against a pre-existing 7-column table on the very first login. `initialize()`
    must add the missing columns instead of silently leaving the old schema."""
    configured = settings(tmp_path)
    with sqlite3.connect(configured.database_path) as connection:
        connection.execute(
            """CREATE TABLE sessions (
            handle_hash BLOB PRIMARY KEY, expires_at INTEGER NOT NULL, cursor TEXT NOT NULL,
            access_nonce BLOB NOT NULL, access_ciphertext BLOB NOT NULL,
            refresh_nonce BLOB NOT NULL, refresh_ciphertext BLOB NOT NULL)"""
        )

    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()

    handle = store.create_session(
        OAuthTokens("access", "refresh", "nonce", patient_uuid="synthetic-patient-uuid"),
        NOW,
        timedelta(minutes=30),
    )

    assert store.access_token(handle, NOW) == "access"
    assert store.patient_uuid(handle, NOW) == "synthetic-patient-uuid"


# --- TICK-051: the chat is a panel, never a landing page (FR-2/FR-31, ADR-8) --------
#
# Every test below drives `Sec-Fetch-Dest` directly rather than through a browser.
# That header is the entire mechanism the destination rule is stated over, so an ASGI
# request carrying it exercises exactly what a real navigation would; the live desktop
# Chrome runs that prove browsers actually send it are recorded under
# evidence/TICK-051/.


async def _completed_authorization(
    app: FastAPI,
    service: AuthorizationService,
    oauth: SyntheticOAuthClient,
    sec_fetch_dest: str | None = "document",
    extra_query: str = "",
) -> httpx.Response:
    """Drive a real launch/callback pair and return the callback's response."""
    location = await service.launch_url(NOW)
    query = parse_qs(urlparse(location).query)
    oauth.nonce = query["nonce"][0]
    return await request(
        app,
        f"/oauth/callback?code=synthetic-code&state={query['state'][0]}{extra_query}",
        sec_fetch_dest=sec_fetch_dest,
    )


def _authorization_app(
    tmp_path: Path,
) -> tuple[AuthSettings, FastAPI, AuthorizationService, SyntheticOAuthClient]:
    configured = settings(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    oauth = SyntheticOAuthClient("placeholder")
    service = AuthorizationService(configured, store, oauth)
    return configured, create_app(configured, service, lambda: NOW), service, oauth


def test_ac2_a_top_level_authorization_lands_on_the_portal_dashboard(tmp_path: Path) -> None:
    """TICK-051 AC2: the reported bug. Entering credentials at top level -- which is
    where TICK-045's breakout script puts the patient -- used to end on the standalone
    chat page full-screen, with the portal gone. It must end on the dashboard.
    """
    configured, app, service, oauth = _authorization_app(tmp_path)

    callback = asyncio.run(_completed_authorization(app, service, oauth))

    assert callback.status_code == 303
    assert callback.headers["location"] == configured.dashboard_redirect_uri
    assert "ai_session" in callback.headers["set-cookie"]


def test_ac5_an_in_panel_authorization_loads_the_chat_and_navigates_nothing(
    tmp_path: Path,
) -> None:
    """TICK-051 AC5: the panel case is the one that must NOT be redirected. A 3xx here
    would send the patient's chat panel to the dashboard and render the portal nested
    inside its own chart.
    """
    _, app, service, oauth = _authorization_app(tmp_path)

    callback = asyncio.run(_completed_authorization(app, service, oauth, sec_fetch_dest="iframe"))

    assert callback.status_code == 200
    assert "location" not in callback.headers
    assert callback.text == CHAT_PAGE_HTML
    # The cookie has to carry every attribute the redirect path sets, or the browser
    # withholds it from the iframe's own fetch("/api/chat") and the panel is inert.
    cookie = callback.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=none" in cookie


@pytest.mark.parametrize("sec_fetch_dest", [None, "empty", "unknown-future-value"])
def test_ac5_an_absent_or_unrecognised_position_is_treated_as_top_level(
    tmp_path: Path, sec_fetch_dest: str | None
) -> None:
    """TICK-051 AC5: absent or unrecognised means top level, because the dashboard
    strands nobody -- a patient sent there is one click from the chat, whereas the
    full-page chat leaves them with the portal gone. Chrome (NFR-19/NFR-35) always
    sends the header, so this path should not run in practice.
    """
    configured, app, service, oauth = _authorization_app(tmp_path)

    callback = asyncio.run(
        _completed_authorization(app, service, oauth, sec_fetch_dest=sec_fetch_dest)
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == configured.dashboard_redirect_uri


def test_ac3_the_callback_discards_every_query_parameter_but_code_and_state(
    tmp_path: Path,
) -> None:
    """TICK-051 AC3: FR-31 and ADR-8 forbid a return-URL parameter, so one appearing on
    this URL must not be able to influence anything. `next` and `redirect` are the two
    names a future change would reach for; `iss` (RFC 9207) is one a real provider
    genuinely sends. All three are dropped, and the destination is unchanged.
    """
    configured, app, service, oauth = _authorization_app(tmp_path)

    callback = asyncio.run(
        _completed_authorization(
            app,
            service,
            oauth,
            extra_query=(
                "&next=https://chat.test/&redirect=https://attacker.test/&iss=https://openemr.test"
            ),
        )
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == configured.dashboard_redirect_uri


def test_ac3_an_unknown_parameter_is_discarded_not_rejected(tmp_path: Path) -> None:
    """TICK-051 AC3: discarded, **not** rejected. Rejecting unknown parameters would
    break the denial path below, which arrives carrying `error_description` and `iss`.
    A session is still issued here -- the extra parameter changed nothing at all.
    """
    configured, app, service, oauth = _authorization_app(tmp_path)

    callback = asyncio.run(
        _completed_authorization(app, service, oauth, extra_query="&unexpected=whatever")
    )

    assert callback.status_code == 303
    assert "ai_session" in callback.headers["set-cookie"]
    assert database_row(configured.database_path, "SELECT count(*) FROM sessions") == (1,)


def test_ac4_a_denied_authorization_returns_to_the_dashboard_without_a_session(
    tmp_path: Path,
) -> None:
    """TICK-051 AC4: declining consent is an expected outcome, not a malformed request.

    The pre-TICK-051 handler declared `code: str, state: str` as required query
    parameters, so OpenEMR's own denial response -- which carries no `code` -- was
    answered with a 422 validation error page on the chat host. The patient who simply
    changed their mind was stranded on it.
    """
    configured, app, _, _ = _authorization_app(tmp_path)

    denial = asyncio.run(
        request(
            app,
            "/oauth/callback?error=access_denied"
            "&error_description=The+user+denied+the+request&state=some-state",
        )
    )

    assert denial.status_code == 303
    assert denial.headers["location"] == configured.dashboard_redirect_uri
    assert "set-cookie" not in denial.headers
    assert database_row(configured.database_path, "SELECT count(*) FROM sessions") == (0,)


def test_ac7_launch_short_circuits_a_live_session_to_the_chat_inside_the_panel(
    tmp_path: Path,
) -> None:
    """TICK-051 AC7: `/oauth/launch` is the panel's `src`, so it is hit every time the
    patient opens the panel. With a valid `ai_session` it must skip the authorization
    round trip entirely rather than re-running the whole OAuth dance.
    """
    configured, app, _, _ = _authorization_app(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    handle = store.create_session(
        OAuthTokens("synthetic-access", "synthetic-refresh", "nonce"),
        NOW,
        configured.session_ttl,
    )

    launch = asyncio.run(request(app, "/oauth/launch", sec_fetch_dest="iframe", cookie=handle))

    assert launch.status_code == 200
    assert launch.text == CHAT_PAGE_HTML


def test_ac7_a_live_session_at_top_level_gets_the_dashboard_not_the_full_page_chat(
    tmp_path: Path,
) -> None:
    """TICK-051 AC7: the short-circuit obeys the same rule as the callback and is not
    an exception to it. Serving the standalone chat here would reintroduce exactly the
    bug this ticket exists to fix, by a second route.
    """
    configured, app, _, _ = _authorization_app(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    handle = store.create_session(
        OAuthTokens("synthetic-access", "synthetic-refresh", "nonce"),
        NOW,
        configured.session_ttl,
    )

    launch = asyncio.run(request(app, "/oauth/launch", cookie=handle))

    assert launch.status_code == 303
    assert launch.headers["location"] == configured.dashboard_redirect_uri


def test_ac7_launch_still_authorizes_when_the_session_is_absent_or_expired(
    tmp_path: Path,
) -> None:
    """TICK-051 AC7: the short-circuit is a skip, not a replacement. Without a valid
    session -- none at all, or one that has expired, which is TICK-045's
    re-authentication path -- the full PKCE launch still runs.
    """
    configured, app, _, _ = _authorization_app(tmp_path)
    store = SessionStore(configured.database_path, configured.encryption_key)
    expired = store.create_session(
        OAuthTokens("synthetic-access", "synthetic-refresh", "nonce"),
        NOW - timedelta(hours=1),
        timedelta(minutes=30),
    )

    no_cookie = asyncio.run(request(app, "/oauth/launch", sec_fetch_dest="iframe"))
    stale = asyncio.run(request(app, "/oauth/launch", sec_fetch_dest="iframe", cookie=expired))

    for response in (no_cookie, stale):
        assert response.status_code == 302
        assert response.headers["location"].startswith(configured.authorize_url)


def test_ac4_a_denial_inside_the_panel_stays_in_the_panel(tmp_path: Path) -> None:
    """TICK-051 AC4, at the position AC4 does not name.

    AC4 is written from the top-level case, which is the only one where the patient
    actually goes anywhere. Applied in the panel, "redirect to the dashboard" would
    render the portal nested inside its own chart -- the failure ADR-8's narrative
    calls out. So a denial resolves through the same `_panel_or_dashboard` every other
    outcome does: the panel keeps the chat, and no session is issued.

    The patient is not stranded either way. They are still on the dashboard with the
    panel open, and a session-less panel is exactly the state TICK-046's fallback
    banner exists to explain.
    """
    configured, app, _, _ = _authorization_app(tmp_path)

    denial = asyncio.run(
        request(
            app,
            "/oauth/callback?error=access_denied&state=some-state",
            sec_fetch_dest="iframe",
        )
    )

    assert denial.status_code == 200
    assert "location" not in denial.headers
    assert "set-cookie" not in denial.headers
    assert database_row(configured.database_path, "SELECT count(*) FROM sessions") == (0,)
