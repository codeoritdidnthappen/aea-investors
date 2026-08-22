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
    # Where a patient lands once an authorization completes at top level: the portal
    # dashboard (FR-31, ADR-8). Read only by main.py's `/oauth/callback` and
    # `/oauth/launch`, and never compared against an `Origin` header.
    #
    # This and `chat_origin` below are deliberately two settings and are **not**
    # interchangeable, however similar they look. They were one value
    # (`AI_SESSION_SUCCESS_REDIRECT_URI`) until TICK-051, which meant repointing the
    # destination at the dashboard would also have made `emr.localhost` the only origin
    # allowed to call `POST /api/chat` -- 403-ing the chat page's own fetch on every
    # turn. Splitting them is what makes the destination safe to change.
    dashboard_redirect_uri: str
    # The one origin the chat page is served from, and so the only origin `POST
    # /api/chat` accepts (main.py's `_chat_origin`). This is the *only* CSRF defense on
    # that route -- the AI session cookie is `SameSite=None` so it survives the
    # cross-site portal iframe, which means the cookie alone proves nothing about who
    # sent the request. Never used as a redirect destination.
    chat_origin: str
    # The OpenEMR portal's own origin, when the chat is embedded in one. Logout is the
    # only route that accepts it (main.py's `/api/logout`), and only because the portal
    # session cookie is `SameSite=Strict` (verified live, evidence/TICK-055): a
    # sign-out click has to stay a same-site top-level navigation to `portal/logout.php`
    # or the portal session is not ended at all, so the AI session must be ended by a
    # cross-origin call made *from* the portal page rather than by redirecting through
    # the chat origin. `None` leaves logout chat-origin-only.
    portal_origin: str | None = None
    cookie_name: str = "ai_session"
    session_ttl: timedelta = timedelta(hours=8)
    state_ttl: timedelta = timedelta(minutes=10)
    # How long before `session_ttl` lands the patient is told the session is ending.
    # The TTL is deliberately absolute (TICK-055 AC6: the stored refresh token is never
    # redeemed, see evidence/TICK-055/DECISIONS.md), so the cut is announced rather
    # than avoided.
    expiry_warning_window: timedelta = timedelta(minutes=30)
    # An expired row is deleted lazily by `active_session`, but only if the handle is
    # ever presented again -- an abandoned session is never revisited, so its encrypted
    # tokens would otherwise sit in SQLite forever (NFR-31/NFR-33). main.py's lifespan
    # sweeps on startup and then on this interval.
    sweep_interval: timedelta = timedelta(hours=1)
    # Patient-context (`patient/*`) only -- never `user/*` (TICK-033). A `user/*` scope
    # forces OpenEMR's registration flow to treat this confidential client as needing
    # the full staff resource-permission consent screen; a genuine patient should never
    # see that for a scheduling chat assistant (evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md,
    # finding 2). `api:oemr`/`api:fhir`/`api:port` are the bare umbrella scopes the
    # Standard/FHIR/Portal API surfaces require; the rest map one-to-one to what this
    # server actually calls on the patient's behalf: `patient/Patient.read`
    # (read-only FHIR patient lookups), `patient/Appointment.read`
    # (`ai_server/openemr/adapter.py`'s FHIR appointment list), `patient/appointment.c`/
    # `.u` (`ai_server/scheduling/booking.py`'s and `cancel.py`'s module-added
    # book/cancel routes, TICK-040/TICK-036), `patient/assessment.{c,r,u}`
    # (`ai_server/onboarding/draft_client.py`'s module-added assessment-draft route),
    # and `patient/demographics.u` (`ai_server/openemr/demographics.py`'s module-added
    # demographics-write route, TICK-042). See evidence/TICK-033/OAUTH_SCOPE_EVIDENCE.md
    # for the live proof and two related, unfixed upstream findings this scope change
    # surfaced.
    scopes: tuple[str, ...] = (
        "openid",
        "offline_access",
        "api:oemr",
        "api:fhir",
        "api:port",
        "patient/Patient.read",
        "patient/Appointment.read",
        "patient/appointment.c",
        "patient/appointment.u",
        "patient/assessment.c",
        "patient/assessment.r",
        "patient/assessment.u",
        "patient/demographics.u",
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
            # Both required, and both new in TICK-051. The single
            # `AI_SESSION_SUCCESS_REDIRECT_URI` they replace was deliberately *renamed*
            # rather than kept and re-meant: this check only fires on a missing
            # variable, so a deployment that added the new chat-origin setting and
            # forgot to repoint the old redirect target would have booted cleanly with
            # the patient still landing on the full-page chat and nothing anywhere
            # saying so. Under the new names that deployment fails to start.
            "AI_SESSION_DASHBOARD_REDIRECT_URI",
            "AI_SESSION_CHAT_ORIGIN",
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
        # Applied to both halves of the TICK-051 split, not just one. A relative
        # dashboard URL would send the patient to a path on the chat host instead of
        # the portal; a relative chat origin would never match a browser's `Origin`
        # header and would 403 every legitimate chat turn forever. Both fail loudly
        # here at startup instead.
        dashboard_redirect_uri = _absolute_url(
            "AI_SESSION_DASHBOARD_REDIRECT_URI", str(values["AI_SESSION_DASHBOARD_REDIRECT_URI"])
        )
        chat_origin = _absolute_url("AI_SESSION_CHAT_ORIGIN", str(values["AI_SESSION_CHAT_ORIGIN"]))
        # Optional, unlike the eleven above: a deployment that does not embed the chat
        # in an OpenEMR portal has no second origin to trust, and leaving this unset
        # keeps logout chat-origin-only rather than failing startup.
        portal_origin = os.environ.get("AI_SESSION_PORTAL_ORIGIN") or None
        if portal_origin is not None:
            # Same reason as chat_origin above: main.py compares this against an Origin
            # header, and a relative value would silently never match, turning the
            # portal sign-out hook into the exact silent no-op TICK-055 exists to
            # remove.
            portal_origin = _absolute_url("AI_SESSION_PORTAL_ORIGIN", portal_origin)
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
            dashboard_redirect_uri=dashboard_redirect_uri,
            chat_origin=chat_origin,
            portal_origin=portal_origin,
        )


def _absolute_url(name: str, value: str) -> str:
    """Return `value` unchanged, or fail startup if it is not an absolute URL.

    Shared by every setting main.py either redirects to or compares against an
    `Origin` header. A relative value in any of them fails silently and permanently at
    request time -- a redirect to the wrong host, or a 403 on every chat turn -- so it
    has to be caught here, at boot, where the error names the variable.
    """
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"{name} must be an absolute URL")
    return value


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    id_token_nonce: str
    # The bound patient's OpenEMR id, when the ID token exposed one (TICK-028's
    # `evidence/TICK-028/BINDING_MATRIX.md`: this OpenEMR version confirms the bound
    # patient via the ID token's `fhirUser`/`sub` claims, not a top-level `patient`
    # token-response field). Optional and defaulted so an ID token without either
    # claim still yields a usable session -- callers that need it (onboarding
    # completion) check for `None` rather than this failing the whole exchange.
    patient_uuid: str | None = None


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
        nonce, patient_uuid = await self._validated_claims(id_token)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token_nonce=nonce,
            patient_uuid=patient_uuid,
        )

    async def _validated_claims(self, token: object) -> tuple[str, str | None]:
        """Verify the OpenID Connect ID token, then return its nonce and patient id.

        The patient id is best-effort (see `OAuthTokens.patient_uuid`): its absence
        does not fail an otherwise-valid, signature-verified token.
        """
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
        return nonce, _patient_uuid_from_claims(claims)


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


def _patient_uuid_from_claims(claims: dict[str, object]) -> str | None:
    """Extract the bound patient id from `fhirUser` (preferred) or `sub`, if either
    claim is present (`evidence/TICK-028/BINDING_MATRIX.md`). `fhirUser` is a reference
    like
    `Patient/<uuid>`; only the id segment is kept. Returns `None` rather than raising --
    every caller treats an unresolved patient id as "onboarding completion unavailable
    for this session", not as an invalid login.
    """
    fhir_user = claims.get("fhirUser")
    if isinstance(fhir_user, str) and fhir_user:
        return fhir_user.rsplit("/", 1)[-1]
    sub = claims.get("sub")
    if isinstance(sub, str) and sub:
        return sub
    return None


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
                refresh_nonce BLOB NOT NULL, refresh_ciphertext BLOB NOT NULL,
                patient_nonce BLOB, patient_ciphertext BLOB)"""
            )
            # `CREATE TABLE IF NOT EXISTS` above is a no-op against a `sessions` table
            # that already exists from before patient_nonce/patient_ciphertext were
            # added (confirmed live: create_session's 9-value INSERT against a
            # pre-existing 7-column table raised sqlite3.OperationalError on the very
            # first login after this column pair shipped) -- add them if missing.
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            for column in ("patient_nonce", "patient_ciphertext"):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} BLOB")

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
        patient_nonce: bytes | None = None
        patient_ciphertext: bytes | None = None
        if tokens.patient_uuid:
            patient_nonce = os.urandom(12)
            patient_ciphertext = self._cipher.encrypt(
                patient_nonce, tokens.patient_uuid.encode("utf-8"), handle_hash + b":patient"
            )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    handle_hash,
                    _timestamp(now + ttl),
                    "",
                    access_nonce,
                    access,
                    refresh_nonce,
                    refresh,
                    patient_nonce,
                    patient_ciphertext,
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

    def delete_session(self, handle: str) -> bool:
        """Delete one session row outright, returning whether a row was actually there.

        Deliberately unconditional on `expires_at`: logout must remove the encrypted
        access and refresh tokens, not merely mark the session dead, so an already
        expired-but-unswept row is removed too (TICK-055 AC1). No `now` argument for the
        same reason -- there is no clock at which a logged-out session should survive.
        """
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM sessions WHERE handle_hash = ?", (_hash(handle),)
            )
        return result.rowcount > 0

    def expires_at(self, handle: str, now: datetime) -> datetime | None:
        """Return when an active session's absolute TTL lands, or `None` if it is
        absent or already expired.

        The TTL is stamped once by `create_session` and never extended (TICK-055 AC6),
        so this is a fixed wall-clock instant the chat turn can warn the patient about
        before it cuts the conversation off.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM sessions WHERE handle_hash = ?", (_hash(handle),)
            ).fetchone()
        if row is None or row[0] <= _timestamp(now):
            return None
        return datetime.fromtimestamp(row[0], tz=timezone.utc)

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

    def access_token(self, handle: str, now: datetime) -> str | None:
        """Decrypt and return the delegated OpenEMR access token for an active session.

        This is the only place the plaintext access token exists outside this call's
        own stack frame; nothing here caches or logs it (TICK-035, onboarding turn
        handling and any other future in-process OpenEMR caller needs a live token
        the same way `active_session`/`load_cursor` already need a live cursor).
        """
        handle_hash = _hash(handle)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT access_nonce, access_ciphertext, expires_at FROM sessions "
                "WHERE handle_hash = ?",
                (handle_hash,),
            ).fetchone()
        if row is None or row[2] <= _timestamp(now):
            return None
        try:
            return self._cipher.decrypt(row[0], row[1], handle_hash + b":access").decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            return None

    def patient_uuid(self, handle: str, now: datetime) -> str | None:
        """Decrypt and return the session's bound patient id, or `None` if never
        captured (`OAuthTokens.patient_uuid`) or the session is absent/expired."""
        handle_hash = _hash(handle)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT patient_nonce, patient_ciphertext, expires_at FROM sessions "
                "WHERE handle_hash = ?",
                (handle_hash,),
            ).fetchone()
        if row is None or row[2] <= _timestamp(now) or row[0] is None or row[1] is None:
            return None
        try:
            return self._cipher.decrypt(row[0], row[1], handle_hash + b":patient").decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            return None

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
