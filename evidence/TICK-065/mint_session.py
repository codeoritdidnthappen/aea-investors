"""Mint one active AI-session handle in the running server's own SQLite database.

TICK-065's live verification needs a signed-in patient, and the OAuth round trip needs a
browser. This creates the row that round trip would have created -- the same
`SessionStore.create_session` call `AuthorizationService.callback` makes -- and prints
the handle, which is exactly what the browser would hold in its `ai_session` cookie.

Nothing about the outage path is faked by doing this: the session is real, the row is the
production shape, and the server reads it through its ordinary `active_session` check.
"""

from __future__ import annotations

import os
from datetime import timedelta

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore, utc_now


def main() -> None:
    settings = AuthSettings.from_environment()
    store = SessionStore(settings.database_path, settings.encryption_key)
    store.initialize()
    tokens = OAuthTokens(
        access_token=os.environ.get("LIVE_ACCESS_TOKEN", "synthetic-access"),
        refresh_token="synthetic-refresh",
        id_token_nonce="synthetic-nonce",
        patient_uuid=os.environ.get("LIVE_PATIENT_UUID"),
    )
    print(store.create_session(tokens, utc_now(), timedelta(hours=1)))


if __name__ == "__main__":
    main()
