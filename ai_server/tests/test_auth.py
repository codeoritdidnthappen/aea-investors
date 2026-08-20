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
        success_redirect_uri="https://chat.test/",
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
        "AI_SESSION_SUCCESS_REDIRECT_URI": "https://chat.test/",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert AuthSettings.from_environment().encryption_key == b"k" * 32


def test_ac1_environment_settings_require_an_absolute_success_redirect_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # main.py's /api/chat Origin check derives its expected origin from this value;
    # a relative URL must fail loudly here, not 403 every request forever.
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
        "AI_SESSION_SUCCESS_REDIRECT_URI": "/",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(
        RuntimeError, match="AI_SESSION_SUCCESS_REDIRECT_URI must be an absolute URL"
    ):
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


async def request(app: FastAPI, path: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            follow_redirects=False,
        ) as client:
            return await client.get(path)


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
        assert callback.headers["location"] == configured.success_redirect_uri
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
