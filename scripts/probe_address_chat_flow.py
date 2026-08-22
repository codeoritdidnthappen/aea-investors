"""Drive the TICK-050 conversational address update against the *live* local stack.

The unit tests in `ai_server/tests/test_address_chat.py` run the same flow against a
synthetic OpenEMR (`httpx.MockTransport`). This probe runs it against the real one: a
real patient-context bearer token, real Caddy TLS, the real module-added Portal route
`PUT /portal/patient/demographics`, and a real MariaDB row underneath -- which is what
the ticket's "verify live against the local Docker stack with a real patient session,
not only against stubs" asks for.

The token is not minted here. It is read out of the running AI server's own session
database, which is where a genuine browser login through OpenEMR's OAuth
authorization_code + PKCE flow already deposited one. `SessionStore` stores only a
*hash* of the opaque cookie handle, but that hash is also the AES-GCM associated data,
so the stored row is self-sufficient: no raw cookie value is needed, and none is
recovered. `scripts/probe_patient_context.py` remains the way to obtain a fresh token
interactively when no live session exists.

Usage (from the repo root, with the local stack up). `PYTHONPATH=.` because this
project is run from the source tree rather than installed (see `pyproject.toml`'s
`pythonpath` setting for pytest):

    docker cp local-ai-server-1:/data/ai_session.sqlite3 /tmp/ai_session.sqlite3
    export AI_SESSION_ENCRYPTION_KEY="$(docker exec local-ai-server-1 \
        printenv AI_SESSION_ENCRYPTION_KEY)"
    export OPENEMR_PORTAL_BASE_URL="https://emr.localhost/apis/default"
    PYTHONPATH=. uv run python scripts/probe_address_chat_flow.py \
        --database /tmp/ai_session.sqlite3

An AI session outlives the OpenEMR access token inside it, so a database copied from
an idle stack usually holds an expired token and every write 401s. Mint a fresh one
with the refresh_token grant and pass it as `AI_PROBE_ACCESS_TOKEN` instead; OpenEMR
rejects that grant without an explicit `scope`, so pass `AuthSettings.scopes` verbatim.
`evidence/TICK-050/ADDRESS_CHAT_FLOW_EVIDENCE.md` records the exact call.

Nothing here may be reused by product code: it reads the session database directly and
disables TLS verification for Caddy's internal CA, neither of which the application
does.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ai_server.app.address_chat import AddressChatService
from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter

HANDLE = "probe-handle"


class _StaticSessionStore:
    """The only part of `SessionStore` `AddressChatService` actually uses."""

    def __init__(self, token: str) -> None:
        self._token = token

    def access_token(self, handle: str, now: datetime) -> str | None:
        del handle, now
        return self._token


def _load_access_token(database: Path, key: bytes) -> str:
    """Decrypt the newest still-active session's OpenEMR access token."""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT handle_hash, access_nonce, access_ciphertext, expires_at "
            "FROM sessions ORDER BY expires_at DESC"
        ).fetchall()
    finally:
        connection.close()
    now = int(datetime.now(timezone.utc).timestamp())
    cipher = AESGCM(key)
    for handle_hash, nonce, ciphertext, expires_at in rows:
        if expires_at <= now:
            continue
        try:
            return cipher.decrypt(nonce, ciphertext, handle_hash + b":access").decode("utf-8")
        except Exception:  # noqa: BLE001 - a stale key just means "try the next row"
            continue
    raise SystemExit(
        "no active AI session found; log in through the chat UI once, then re-copy "
        "the session database"
    )


async def _drive(service: AddressChatService, turns: list[str]) -> None:
    for turn in turns:
        reply = "".join([chunk async for chunk in service.stream_reply(HANDLE, turn)])
        print(f"\n>>> patient: {turn}")
        print(f"<<< assistant: {reply}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--street", default="742 Evergreen Terrace")
    parser.add_argument("--unit", default="Apt 7C")
    parser.add_argument("--city", default="Springfield")
    parser.add_argument("--state", default="IL")
    parser.add_argument("--zip-code", dest="zip_code", default="62704")
    parser.add_argument(
        "--env-key",
        default="AI_SESSION_ENCRYPTION_KEY",
        help="environment variable holding the base64 session encryption key",
    )
    args = parser.parse_args()

    import os

    portal_base_url = os.environ.get("OPENEMR_PORTAL_BASE_URL")
    if not portal_base_url:
        raise SystemExit("set OPENEMR_PORTAL_BASE_URL (see the docstring)")
    # An AI session outlives the OpenEMR access token inside it (the session TTL is
    # 30 minutes of *chat*, the bearer token expires on OpenEMR's own schedule), so a
    # session database copied from a stack that has been idle usually holds an expired
    # token. AI_PROBE_ACCESS_TOKEN lets a freshly refreshed one be supplied instead;
    # see the evidence file for the refresh_token grant that mints it.
    supplied = os.environ.get("AI_PROBE_ACCESS_TOKEN")
    if supplied:
        token = supplied
        print(f"using the supplied patient-context token ({len(token)} chars)")
    else:
        raw_key = os.environ.get(args.env_key)
        if not raw_key:
            raise SystemExit(f"set {args.env_key} or AI_PROBE_ACCESS_TOKEN (see the docstring)")
        # Same decoding `AuthSettings.from_environment` uses: base64url, padding tolerated.
        token = _load_access_token(args.database, base64.urlsafe_b64decode(f"{raw_key}==="))
        print(f"decrypted a live patient-context token ({len(token)} chars) from {args.database}")
    print(f"portal base url: {portal_base_url}")

    address = f"{args.street}, {args.unit}, {args.city}, {args.state} {args.zip_code}"
    # Caddy terminates TLS with its internal CA, exactly as the AI server's own
    # in-container client tolerates (`verify=False` in `ai_server/app/main.py`).
    client = httpx.AsyncClient(timeout=30.0, verify=False)
    service = AddressChatService(
        demographics=OpenEmrDemographicsAdapter(
            OpenEmrPortalSettings(portal_base_url=portal_base_url), client
        ),
        session_store=_StaticSessionStore(token),
    )

    async def run() -> None:
        try:
            await _drive(
                service,
                [
                    "I moved",  # trigger
                    f"{args.street}, {args.city}, ZZ 1234",  # invalid -> local rejection
                    address,  # corrected -> parsed echo-back
                    "hmm",  # not a confirmation -> re-shows, writes nothing
                    "confirm",  # the one and only write
                ],
            )
        finally:
            await client.aclose()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
