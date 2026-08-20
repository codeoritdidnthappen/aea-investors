"""OAuth launch handling and encrypted, non-patient AI session storage."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AuthError(Exception):
    """An authorization failure safe to return to the browser."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AuthSettings:
    """Validated configuration for the OAuth callback boundary."""

    database_path: Path
    encryption_key: bytes
    authorize_url: str
    token_url: str
    jwks_url: str
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    success_redirect_uri: str
    cookie_name: str = "ai_session"
    session_ttl: timedelta = timedelta(hours=8)
    state_ttl: timedelta = timedelta(minutes=10)
    scopes: tuple[str, ...] = (
        "openid",
        "offline_access",
        "api:oemr",
        "user/patient.crus",
        "user/appointment.cruds",
    )

    @classmethod
    def from_environment(cls) -> AuthSettings:
        """Parse all required deployment settings once during application startup."""
        required = (
            "AI_SESSION_DATABASE_PATH",
            "AI_SESSION_ENCRYPTION_KEY",
            "OPENEMR_OAUTH_AUTHORIZE_URL",
            "OPENEMR_OAUTH_TOKEN_URL",
            "OPENEMR_OAUTH_JWKS_URL",
            "OPENEMR_OAUTH_ISSUER",
            "OPENEMR_OAUTH_CLIENT_ID",
            "OPENEMR_OAUTH_CLIENT_SECRET",
            "OPENEMR_OAUTH_REDIRECT_URI",
            "AI_SESSION_SUCCESS_REDIRECT_URI",
        )
        values = {name: os.environ.get(name) for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"missing required auth settings: {', '.join(missing)}")
        try:
            key = base64.urlsafe_b64decode(f"{values['AI_SESSION_ENCRYPTION_KEY']}===")
        except ValueError as exc:
            raise RuntimeError("AI_SESSION_ENCRYPTION_KEY must be base64url encoded") from exc
        if len(key) != 32:
            raise RuntimeError("AI_SESSION_ENCRYPTION_KEY must decode to exactly 32 bytes")
        success_redirect_uri = str(values["AI_SESSION_SUCCESS_REDIRECT_URI"])
        parsed_success_redirect = urlsplit(success_redirect_uri)
        if not parsed_success_redirect.scheme or not parsed_success_redirect.netloc:
            # main.py's /api/chat Origin check derives the chat page's expected
            # origin from this value; a relative URL would make every legitimate
            # request 403 forever instead of failing loudly here at startup.
            raise RuntimeError("AI_SESSION_SUCCESS_REDIRECT_URI must be an absolute URL")
        return cls(
            database_path=Path(str(values["AI_SESSION_DATABASE_PATH"])),
            encryption_key=key,
            authorize_url=str(values["OPENEMR_OAUTH_AUTHORIZE_URL"]),
            token_url=str(values["OPENEMR_OAUTH_TOKEN_URL"]),
            jwks_url=str(values["OPENEMR_OAUTH_JWKS_URL"]),
            issuer=str(values["OPENEMR_OAUTH_ISSUER"]),
            client_id=str(values["OPENEMR_OAUTH_CLIENT_ID"]),
            client_secret=str(values["OPENEMR_OAUTH_CLIENT_SECRET"]),
            redirect_uri=str(values["OPENEMR_OAUTH_REDIRECT_URI"]),
            success_redirect_uri=success_redirect_uri,
        )


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    id_token_nonce: str


class OAuthClient(Protocol):
    async def exchange(self, code: str, verifier: str) -> OAuthTokens: ...


class OpenEmrOAuthClient:
    """Exchange a code with the configured OpenEMR OAuth endpoint over TLS."""

    def __init__(self, settings: AuthSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def exchange(self, code: str, verifier: str) -> OAuthTokens:
        response = await self._client.post(
            self._settings.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "code_verifier": verifier,
            },
        )
        if response.status_code != 200:
            raise AuthError("authorization code exchange failed", 401)
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise AuthError("authorization server returned an invalid token response", 401)
        try:
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
            id_token = payload["id_token"]
        except KeyError as exc:
            raise AuthError(
                "authorization server returned an incomplete token response", 401
            ) from exc
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise AuthError("authorization server returned an invalid token response", 401)
        nonce = await self._validated_jwt_nonce(id_token)
        return OAuthTokens(
            access_token=access_token, refresh_token=refresh_token, id_token_nonce=nonce
        )

    async def _validated_jwt_nonce(self, token: object) -> str:
        """Verify the OpenID Connect ID token before trusting its nonce claim."""
        header, claims, signed_data, signature = _jwt_parts(token)
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm != "RS256" or (key_id is not None and not isinstance(key_id, str)):
            raise AuthError("authorization server returned an unsupported ID token", 401)
        jwks_response = await self._client.get(self._settings.jwks_url)
        if jwks_response.status_code != 200:
            raise AuthError("authorization server keys are unavailable", 401)
        jwks: object = jwks_response.json()
        keys = _jwks_keys(jwks, key_id)
        if not _verify_with_any_key(keys, signature, signed_data):
            raise AuthError("authorization server returned an invalid ID token", 401)
        _validate_id_token_claims(claims, self._settings, utc_now())
        nonce = claims.get("nonce")
        if not isinstance(nonce, str):
            raise AuthError("authorization server returned an ID token without a nonce", 401)
        return nonce


def _jwt_parts(token: object) -> tuple[dict[str, object], dict[str, object], bytes, bytes]:
    if not isinstance(token, str):
        raise AuthError("authorization server returned an invalid ID token", 401)
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("authorization server returned an invalid ID token", 401)
    try:
        header = json.loads(_decode_base64url(parts[0]))
        claims = json.loads(_decode_base64url(parts[1]))
        signature = _decode_base64url(parts[2])
    except (UnicodeDecodeError, ValueError, binascii.Error) as exc:
        raise AuthError("authorization server returned an invalid ID token", 401) from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AuthError("authorization server returned an invalid ID token", 401)
    return header, claims, f"{parts[0]}.{parts[1]}".encode("ascii"), signature


def _jwks_keys(jwks: object, key_id: str | None) -> list[rsa.RSAPublicKey]:
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise AuthError("authorization server returned invalid signing keys", 401)
    candidates = jwks["keys"]
    # OpenEMR's OIDC responses omit `kid` from both the ID token header and its JWKS
    # (confirmed live: steverhoades/oauth2-openid-connect-server's IdTokenResponse
    # only sets `kid` when constructed with a $keyIdentifier, which OpenEMR never
    # passes). Without a kid to filter by, collect every published RSA key and let
    # the caller's signature check -- not a count -- decide which one actually
    # signed this token; that stays correct through a key rotation too.
    if key_id is not None:
        candidates = [c for c in candidates if isinstance(c, dict) and c.get("kid") == key_id]
    keys: list[rsa.RSAPublicKey] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        modulus = candidate.get("n")
        exponent = candidate.get("e")
        if (
            candidate.get("kty") != "RSA"
            or not isinstance(modulus, str)
            or not isinstance(exponent, str)
        ):
            continue
        try:
            keys.append(
                rsa.RSAPublicNumbers(
                    int.from_bytes(_decode_base64url(exponent), "big"),
                    int.from_bytes(_decode_base64url(modulus), "big"),
                ).public_key()
            )
        except (ValueError, binascii.Error) as exc:
            raise AuthError("authorization server returned an invalid signing key", 401) from exc
    if not keys:
        raise AuthError("authorization server signing key was not found", 401)
    return keys


def _verify_with_any_key(
    keys: list[rsa.RSAPublicKey], signature: bytes, signed_data: bytes
) -> bool:
    for key in keys:
        try:
            key.verify(signature, signed_data, padding.PKCS1v15(), hashes.SHA256())
            return True
        except InvalidSignature:
            continue
    return False


def _validate_id_token_claims(
    claims: dict[str, object], settings: AuthSettings, now: datetime
) -> None:
    audience = claims.get("aud")
    audience_matches = audience == settings.client_id or (
        isinstance(audience, list) and settings.client_id in audience
    )
    expires_at = claims.get("exp")
    if (
        claims.get("iss") != settings.issuer
        or not audience_matches
        or not isinstance(expires_at, int)
        or expires_at <= _timestamp(now)
    ):
        raise AuthError("authorization server returned invalid ID token claims", 401)


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


class SessionStore:
    """SQLite WAL store limited to encrypted tokens and non-patient session plumbing."""

    def __init__(self, database_path: Path, key: bytes) -> None:
        self._database_path = database_path
        self._cipher = AESGCM(key)

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pending_authorizations (
                state_hash BLOB PRIMARY KEY, nonce_hash BLOB NOT NULL, verifier_nonce BLOB NOT NULL,
                verifier_ciphertext BLOB NOT NULL, expires_at INTEGER NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                handle_hash BLOB PRIMARY KEY, expires_at INTEGER NOT NULL, cursor TEXT NOT NULL,
                access_nonce BLOB NOT NULL, access_ciphertext BLOB NOT NULL,
                refresh_nonce BLOB NOT NULL, refresh_ciphertext BLOB NOT NULL)"""
            )

    def create_pending(self, now: datetime, ttl: timedelta) -> tuple[str, str, str]:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        verifier_nonce = os.urandom(12)
        expires_at = _timestamp(now + ttl)
        ciphertext = self._cipher.encrypt(verifier_nonce, verifier.encode("utf-8"), _hash(state))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO pending_authorizations VALUES (?, ?, ?, ?, ?)",
                (_hash(state), _hash(nonce), verifier_nonce, ciphertext, expires_at),
            )
        return state, nonce, verifier

    def consume_pending(self, state: str, now: datetime) -> tuple[bytes, str]:
        state_hash = _hash(state)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT nonce_hash, verifier_nonce, verifier_ciphertext, expires_at "
                "FROM pending_authorizations WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            connection.execute(
                "DELETE FROM pending_authorizations WHERE state_hash = ?", (state_hash,)
            )
        if row is None or row[3] <= _timestamp(now):
            raise AuthError("authorization state is invalid or expired", 400)
        try:
            verifier = self._cipher.decrypt(row[1], row[2], state_hash).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise AuthError("authorization state could not be validated", 400) from exc
        return row[0], verifier

    def create_session(self, tokens: OAuthTokens, now: datetime, ttl: timedelta) -> str:
        handle = secrets.token_urlsafe(32)
        handle_hash = _hash(handle)
        access_nonce = os.urandom(12)
        refresh_nonce = os.urandom(12)
        access = self._cipher.encrypt(
            access_nonce, tokens.access_token.encode("utf-8"), handle_hash + b":access"
        )
        refresh = self._cipher.encrypt(
            refresh_nonce, tokens.refresh_token.encode("utf-8"), handle_hash + b":refresh"
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    handle_hash,
                    _timestamp(now + ttl),
                    "",
                    access_nonce,
                    access,
                    refresh_nonce,
                    refresh,
                ),
            )
        return handle

    def active_session(self, handle: str, now: datetime) -> bool:
        handle_hash = _hash(handle)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE handle_hash = ?", (handle_hash,)
            ).fetchone()
            if row is not None and row[0] <= _timestamp(now):
                connection.execute("DELETE FROM sessions WHERE handle_hash = ?", (handle_hash,))
                return False
        return row is not None

    def save_cursor(self, handle: str, cursor: str, now: datetime) -> None:
        """Persist the non-patient onboarding workflow cursor for an active session.

        This is never a field value (ARCHITECTURE.md Sec. 5, "AI orchestration"):
        TICK-017's `OnboardingCursor` serializes to exactly the opaque draft id this
        column stores, so a restart can reload the draft from OpenEMR without this
        process ever having kept a patient answer itself.
        """
        handle_hash = _hash(handle)
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET cursor = ? WHERE handle_hash = ? AND expires_at > ?",
                (cursor, handle_hash, _timestamp(now)),
            )

    def load_cursor(self, handle: str, now: datetime) -> str | None:
        """Return the persisted cursor for an active session, or `None` if absent/expired."""
        handle_hash = _hash(handle)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor, expires_at FROM sessions WHERE handle_hash = ?", (handle_hash,)
            ).fetchone()
        if row is None or row[1] <= _timestamp(now) or not row[0]:
            return None
        return row[0]

    def purge_expired(self, now: datetime) -> int:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (_timestamp(now),)
            )
            connection.execute(
                "DELETE FROM pending_authorizations WHERE expires_at <= ?", (_timestamp(now),)
            )
        return result.rowcount

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.timestamp())


class AuthorizationService:
    """Coordinates launch state, code exchange, nonce validation, and sessions."""

    def __init__(self, settings: AuthSettings, store: SessionStore, client: OAuthClient) -> None:
        self._settings = settings
        self._store = store
        self._client = client

    async def launch_url(self, now: datetime) -> str:
        state, nonce, verifier = await asyncio.to_thread(
            self._store.create_pending, now, self._settings.state_ttl
        )
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "scope": " ".join(self._settings.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{self._settings.authorize_url}?{urlencode(params)}"

    async def callback(self, code: str, state: str, now: datetime) -> str:
        expected_nonce_hash, verifier = await asyncio.to_thread(
            self._store.consume_pending, state, now
        )
        tokens = await self._client.exchange(code, verifier)
        if not secrets.compare_digest(_hash(tokens.id_token_nonce), expected_nonce_hash):
            raise AuthError("authorization nonce did not match", 401)
        return await asyncio.to_thread(
            self._store.create_session, tokens, now, self._settings.session_ttl
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
