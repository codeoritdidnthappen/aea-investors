import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from ai_server.app.auth import (
    AuthError,
    AuthorizationService,
    AuthSettings,
    OpenEmrOAuthClient,
    SessionStore,
    utc_now,
)
from ai_server.app.chat import (
    CHAT_PAGE_HTML,
    ChatService,
    ChatTurnRequest,
    NoActionTool,
    unavailable_chat_service,
)
from ai_server.app.health import (
    HealthService,
    HealthSettings,
    default_health_service,
    unavailable_health_service,
)
from ai_server.llm.groq import GroqConfigurationError, GroqSettings, GroqWorkflow, HttpGroqClient
from ai_server.privacy.gate import PrivacyGate


def _origin_of(url: str) -> str:
    """Return the lowercase scheme+host part of `url`, to compare against an Origin header.

    Browsers always send Origin with a lowercase-normalized scheme and host, so this
    must normalize the same way or a config value with any uppercase would 403 every
    legitimate request.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def create_app(
    settings: AuthSettings | None = None,
    authorization: AuthorizationService | None = None,
    clock: Callable[[], datetime] = utc_now,
    health_service: HealthService | None = None,
    chat_service: ChatService | None = None,
) -> FastAPI:
    """Create the AI server without exposing delegated credentials to the browser."""

    configured_settings = settings
    configured_authorization = authorization
    configured_health_service = health_service
    configured_chat_service = chat_service
    configured_session_store: SessionStore | None = None
    owned_http_clients: list[httpx.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal \
            configured_settings, \
            configured_authorization, \
            configured_health_service, \
            configured_chat_service, \
            configured_session_store
        if configured_settings is None:
            configured_settings = AuthSettings.from_environment()
        store = SessionStore(configured_settings.database_path, configured_settings.encryption_key)
        await asyncio.to_thread(store.initialize)
        configured_session_store = store
        if configured_authorization is None:
            # OPENEMR_OAUTH_TOKEN_URL/JWKS_URL (deploy/local/.env.example) call
            # OpenEMR's container directly (`https://openemr/...`), bypassing Caddy,
            # so they hit OpenEMR's own self-signed cert -- the same one
            # deploy/local/Caddyfile already treats as untrusted via
            # `tls_insecure_skip_verify` for the identical reason. No CA is shared
            # into this container, so default verification always fails here
            # (confirmed live: every token exchange 500s on ConnectError), not just
            # in some misconfiguration case.
            auth_http_client = httpx.AsyncClient(timeout=10.0, verify=False)
            owned_http_clients.append(auth_http_client)
            configured_authorization = AuthorizationService(
                configured_settings,
                store,
                OpenEmrOAuthClient(configured_settings, auth_http_client),
            )
        if configured_health_service is None:
            # Same untrusted-self-signed-cert reason as auth_http_client above:
            # HealthSettings' openemr_api probe hits configured_settings.issuer
            # (OPENEMR_OAUTH_ISSUER), which resolves to OpenEMR the same way. The
            # external_llm probe sends a live Groq API key to the public internet, so
            # it needs its own, fully-verified client -- reusing the OpenEMR one here
            # would silently disable TLS verification for that call too.
            health_openemr_client = httpx.AsyncClient(timeout=2.0, verify=False)
            health_groq_client = httpx.AsyncClient(timeout=2.0)
            owned_http_clients.append(health_openemr_client)
            owned_http_clients.append(health_groq_client)
            configured_health_service = default_health_service(
                HealthSettings.from_environment(configured_settings.issuer),
                health_openemr_client,
                health_groq_client,
            )
        if configured_chat_service is None:
            chat_http_client = httpx.AsyncClient(timeout=30.0)
            owned_http_clients.append(chat_http_client)
            configured_chat_service = _build_chat_service(chat_http_client, clock)
        yield
        for owned_client in owned_http_clients:
            await owned_client.aclose()

    server = FastAPI(title="Intake AI Server", version="0.1.0", lifespan=lifespan)

    @server.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @server.get("/health")
    async def health() -> dict[str, object]:
        """Return non-sensitive dependency reachability for local development."""
        service = configured_health_service or unavailable_health_service()
        return await service.report()

    @server.get("/", response_class=HTMLResponse)
    async def chat_page() -> str:
        """Serve the self-contained chat page the OAuth callback redirects to."""
        return CHAT_PAGE_HTML

    @server.post("/api/chat")
    async def chat_turn(request: Request, turn: ChatTurnRequest) -> StreamingResponse:
        """Stream a reply for one turn; refuse any request without an AI session."""
        if configured_settings is None or configured_session_store is None:
            raise AuthError("the chat service is unavailable", 503)
        # SameSite=None on the session cookie (required for the cross-site portal
        # iframe, see /oauth/callback) means the cookie rides along on any origin's
        # request; Starlette also parses the body as JSON regardless of the
        # declared Content-Type, so neither gives CSRF protection on its own. Only
        # a same-origin fetch from the served chat page sends a matching Origin.
        origin = request.headers.get("origin")
        if origin is None or origin.lower() != _origin_of(configured_settings.success_redirect_uri):
            raise AuthError("request origin is not allowed", 403)
        handle = request.cookies.get(configured_settings.cookie_name)
        valid = handle is not None and await asyncio.to_thread(
            configured_session_store.active_session, handle, clock()
        )
        if not valid:
            raise AuthError("an active AI session is required", 401)
        service = configured_chat_service or unavailable_chat_service()
        return StreamingResponse(service.stream_reply(turn.message), media_type="text/plain")

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
            # The chat page is embedded as a cross-site iframe inside the OpenEMR
            # portal (TICK-012): browsers compute SameSite against the top-level
            # document's site, so Lax would silently withhold this cookie from the
            # iframe's own fetch("/api/chat") call. `secure=True` is required for
            # None. This alone permits cross-site delivery, so chat_turn() below
            # enforces an Origin check as the actual CSRF defense.
            samesite="none",
            path="/",
        )
        return response

    return server


def _build_chat_service(client: httpx.AsyncClient, clock: Callable[[], datetime]) -> ChatService:
    """Build the real Groq-backed chat service, or a fixed-unavailable fallback.

    Mirrors `default_health_service`'s tolerance of absent Groq configuration: the
    demo's default path requires no paid LLM service (NFR-20), so a missing
    `GROQ_API_KEY`/ZDR date degrades the chat service instead of failing startup.
    """
    try:
        groq_settings = GroqSettings.from_environment()
    except GroqConfigurationError:
        return unavailable_chat_service()
    workflow = GroqWorkflow(PrivacyGate.create(), HttpGroqClient(groq_settings, client))
    return ChatService(workflow=workflow, tool=NoActionTool(), clock=clock)


app = create_app()
