import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ai_server.app.auth import (
    AuthError,
    AuthorizationService,
    AuthSettings,
    OpenEmrOAuthClient,
    SessionStore,
    utc_now,
)
from ai_server.app.health import (
    HealthService,
    HealthSettings,
    default_health_service,
    unavailable_health_service,
)


def create_app(
    settings: AuthSettings | None = None,
    authorization: AuthorizationService | None = None,
    clock: Callable[[], datetime] = utc_now,
    health_service: HealthService | None = None,
) -> FastAPI:
    """Create the AI server without exposing delegated credentials to the browser."""

    configured_settings = settings
    configured_authorization = authorization
    configured_health_service = health_service
    http_client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal \
            configured_settings, \
            configured_authorization, \
            configured_health_service, \
            http_client
        if configured_settings is None:
            configured_settings = AuthSettings.from_environment()
        store = SessionStore(configured_settings.database_path, configured_settings.encryption_key)
        await asyncio.to_thread(store.initialize)
        if configured_authorization is None:
            http_client = httpx.AsyncClient(timeout=10.0)
            configured_authorization = AuthorizationService(
                configured_settings,
                store,
                OpenEmrOAuthClient(configured_settings, http_client),
            )
        if configured_health_service is None:
            if http_client is None:
                http_client = httpx.AsyncClient(timeout=2.0)
            configured_health_service = default_health_service(
                HealthSettings.from_environment(configured_settings.issuer), http_client
            )
        yield
        if http_client is not None:
            await http_client.aclose()

    server = FastAPI(title="Intake AI Server", version="0.1.0", lifespan=lifespan)

    @server.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @server.get("/health")
    async def health() -> dict[str, object]:
        """Return non-sensitive dependency reachability for local development."""
        service = configured_health_service or unavailable_health_service()
        return await service.report()

    @server.get("/oauth/launch")
    async def oauth_launch() -> RedirectResponse:
        """Start a stateful PKCE authorization-code launch."""
        if configured_authorization is None:
            raise AuthError("authorization service is unavailable", 503)
        return RedirectResponse(await configured_authorization.launch_url(clock()), status_code=302)

    @server.get("/oauth/callback")
    async def oauth_callback(code: str, state: str) -> RedirectResponse:
        """Exchange one authorization code and issue only an opaque AI-session cookie."""
        if configured_authorization is None or configured_settings is None:
            raise AuthError("authorization service is unavailable", 503)
        handle = await configured_authorization.callback(code, state, clock())
        response = RedirectResponse(configured_settings.success_redirect_uri, status_code=303)
        response.set_cookie(
            configured_settings.cookie_name,
            handle,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    return server


app = create_app()
