import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
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
    BookingToolSettings,
    ChatTurnRequest,
    NoMappedCandidateSource,
)
from ai_server.app.health import (
    HealthService,
    HealthSettings,
    default_health_service,
    unavailable_health_service,
)
from ai_server.app.model_turn import (
    ModelTurnService,
    TurnServices,
    unavailable_model_turn_service,
)
from ai_server.llm.general_knowledge import GeneralKnowledgeService
from ai_server.llm.groq import (
    GroqConfigurationError,
    GroqSettings,
    HttpGroqClient,
)
from ai_server.llm.local import (
    HttpLocalModelClient,
    LocalModelConfigurationError,
    LocalModelSettings,
)
from ai_server.llm.provider import GROQ, selected_llm_provider
from ai_server.ocr.service import OcrService, SubprocessTesseractEngine
from ai_server.onboarding.draft_client import AssessmentDraftAdapter, OpenEmrPortalSettings
from ai_server.onboarding.flow import OnboardingFlow
from ai_server.openemr.adapter import (
    OpenEmrConfigurationError,
    OpenEmrScheduleAdapter,
    OpenEmrScheduleSettings,
)
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter
from ai_server.privacy.gate import PrivacyGate
from ai_server.scheduling.appointments import AnonymousAppointmentStore, AppointmentDiscoveryService
from ai_server.scheduling.booking import BookingService, OpenEmrBookingAdapter
from ai_server.scheduling.cancel import AppointmentCancelAdapter, CancellationService
from ai_server.scheduling.slots import AnonymousSlotStore, SlotDiscoveryService


def _origin_of(url: str) -> str:
    """Return the lowercase scheme+host part of `url`, to compare against an Origin header.

    Browsers always send Origin with a lowercase-normalized scheme and host, so this
    must normalize the same way or a config value with any uppercase would 403 every
    legitimate request.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _chat_origin(settings: AuthSettings) -> str:
    """The one origin the chat page is served from, and so the only origin a chat turn
    may come from.

    Reads `chat_origin`, which since TICK-051 is a setting of its own rather than the
    post-login redirect target it used to be derived from. That derivation was the bug:
    pointing the destination at the portal dashboard -- which FR-31 requires -- would
    also have made the dashboard's origin the only one allowed to call
    `POST /api/chat`, 403-ing the chat page's own `fetch()` on every turn.

    This function stays the single place both `chat_turn` and `logout` read the value,
    so there is one definition of "the chat's origin" rather than two that can drift.
    """
    return _origin_of(settings.chat_origin)


# The only query parameters `/oauth/callback` acts on. Stated here, and applied in the
# handler, rather than left to FastAPI's signature binding: FR-31 and ADR-8 forbid any
# `next=`/`redirect=` return-URL parameter, and an allowlist that is visible in the
# code is what stops a later change from quietly honouring one. Everything else --
# RFC 9207's `iss`, OpenEMR's `error_description`, anything a provider adds later --
# is discarded, not rejected: rejecting unknown parameters would break the
# authorization denial path, which is an expected outcome and not a malformed request.
_CALLBACK_HONOURED_PARAMS = frozenset({"code", "state"})


def _is_top_level(request: Request) -> bool:
    """Whether this request is a top-level navigation rather than one into the panel.

    `Sec-Fetch-Dest` is the whole mechanism (ADR-8): browsers send `document` for a
    top-level navigation and `iframe` for a navigation into a frame, so the server can
    resolve its own position with no client-side interstitial and, critically, without
    a return-URL parameter that FR-31 forbids.

    Position is what the rule is stated over, deliberately. "Did the patient type a
    password?" is unknowable here -- a live OAuth2 provider session completes the whole
    exchange with no prompt at all, whether the flow is running at top level or in the
    panel -- but position is knowable, and it is what actually decides whether landing
    on a full-page chat would strand them.

    Absent or unrecognised values are treated as top level, because the dashboard
    strands nobody: a patient sent there is one click from the chat, whereas the
    full-page chat leaves them with the portal gone. Chrome, the only supported target
    (NFR-19, NFR-35), always sends the header, so this default should not run in
    practice.
    """
    return request.headers.get("sec-fetch-dest", "").strip().lower() != "iframe"


def _logout_origins(settings: AuthSettings) -> tuple[str, ...]:
    """The origins allowed to end an AI session.

    The chat origin, exactly as `chat_turn` requires, plus -- only when configured --
    the OpenEMR portal's own origin. That second entry is not a relaxation for
    convenience: the portal session cookie is `SameSite=Strict` (verified live against
    OpenEMR 8.3.0, evidence/TICK-055/PORTAL_LOGOUT_MECHANISM.md), so a sign-out click
    must remain a same-site top-level navigation to `portal/logout.php` or the portal
    session is never destroyed. That forecloses routing sign-out through the chat
    origin, and leaves a cross-origin call made *from* the portal page as the only
    mechanism that ends both sessions. Every such call carries
    `Origin: <portal origin>`, so a chat-origin-only check here would reject it and
    make the whole hook the silent no-op TICK-055 AC2 calls a failure.

    An origin that is neither of these -- any other site the patient has open -- still
    cannot end the session, which is what AC4 requires.
    """
    origins = [_chat_origin(settings)]
    if settings.portal_origin is not None:
        origins.append(_origin_of(settings.portal_origin))
    return tuple(origins)


def _panel_or_dashboard(settings: AuthSettings, request: Request) -> Response:
    """Answer `request` where the patient actually is: the chat in the panel, the
    portal dashboard at top level.

    The one place the FR-31/ADR-8 invariant is expressed, shared by `/oauth/callback`
    and `/oauth/launch`'s short-circuit so the short-circuit cannot become an exception
    to it -- a live session reached at top level must never be answered with the
    full-page chat.

    Note what this does *not* take: any parameter, from the query string or anywhere
    else. The destination is unconditional. It does not vary with the portal page the
    patient was on when their session ended, and no `next=`/`redirect=` value can be
    introduced later without changing this signature.

    The in-panel answer serves the chat page inline rather than redirecting to it. A
    redirect would work, but serving it here keeps the panel on one navigation and
    leaves `GET /` -- the chat's own URL -- as the only route that hands out the
    standalone page.
    """
    if _is_top_level(request):
        return RedirectResponse(settings.dashboard_redirect_uri, status_code=303)
    return HTMLResponse(CHAT_PAGE_HTML)


def _set_session_cookie(response: Response, settings: AuthSettings, handle: str) -> None:
    """Attach the opaque AI-session cookie, with the attributes the iframe requires.

    Every attribute is load-bearing and has to be repeated wherever this cookie is set
    or cleared (see `/api/logout`), or the browser treats it as a different cookie.
    """
    response.set_cookie(
        settings.cookie_name,
        handle,
        httponly=True,
        secure=True,
        # The chat page is embedded as a cross-site iframe inside the OpenEMR portal
        # (TICK-012): browsers compute SameSite against the top-level document's site,
        # so Lax would silently withhold this cookie from the iframe's own
        # fetch("/api/chat") call. `secure=True` is required for None. This alone
        # permits cross-site delivery, so chat_turn() enforces an Origin check --
        # against `chat_origin`, not the redirect target -- as the actual CSRF defense.
        samesite="none",
        path="/",
    )


async def _sweep_expired_sessions(
    store: SessionStore, clock: Callable[[], datetime], interval_seconds: float
) -> None:
    """Delete expired session rows forever, every `interval_seconds`.

    `active_session` only deletes an expired row when its handle is presented again,
    and the `ai_session` cookie has no `max_age`, so closing the browser strands the
    row and its encrypted tokens in SQLite permanently (NFR-31/NFR-33, TICK-055 AC5).
    Startup alone would not bound that for a container that runs for weeks.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await asyncio.to_thread(store.purge_expired, clock())


def _expiry_notice(expires_at: datetime | None, now: datetime, window: timedelta) -> str | None:
    """Warn the patient once their session is inside its final `window`, else `None`.

    The AI session's TTL is absolute: `create_session` stamps `expires_at` once and no
    code path ever extends it (TICK-055 AC6 keeps it that way, deliberately). A patient
    mid-conversation is therefore cut off at a fixed instant, and being cut off without
    warning is the part that is unacceptable, not the cut itself.
    """
    if expires_at is None:
        return None
    remaining = expires_at - now
    if remaining > window:
        return None
    minutes = max(1, int(remaining.total_seconds() // 60))
    return (
        f"Heads up: this chat session ends in about {minutes} "
        f"minute{'' if minutes == 1 else 's'}, and you'll need to sign in from your "
        "patient portal again to keep chatting.\n\n"
    )


def _with_notice(notice: str | None, stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Prefix a streamed reply with a plain-text notice, if there is one."""
    if notice is None:
        return stream

    async def prefixed() -> AsyncIterator[str]:
        yield notice
        async for chunk in stream:
            yield chunk

    return prefixed()


def create_app(
    settings: AuthSettings | None = None,
    authorization: AuthorizationService | None = None,
    clock: Callable[[], datetime] = utc_now,
    health_service: HealthService | None = None,
    model_turn_service: ModelTurnService | None = None,
) -> FastAPI:
    """Create the AI server without exposing delegated credentials to the browser.

    There is no `onboarding_service`/`address_service` parameter, and there is no way to
    inject one. TICK-065 deleted `OnboardingChatService` and `AddressChatService`
    outright (D12): the model owns every turn, and a deterministic handler that no turn
    reaches is a path that rots and then produces a bad parse in a chart at the worst
    possible moment. The signature is the enforcement -- a later change cannot reinstate
    a phrase-matched bypass of the model without adding a parameter back here.
    """

    configured_settings = settings
    configured_authorization = authorization
    configured_health_service = health_service
    configured_model_turn_service = model_turn_service
    configured_session_store: SessionStore | None = None
    owned_http_clients: list[httpx.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal \
            configured_settings, \
            configured_authorization, \
            configured_health_service, \
            configured_model_turn_service, \
            configured_session_store
        if configured_settings is None:
            configured_settings = AuthSettings.from_environment()
        store = SessionStore(configured_settings.database_path, configured_settings.encryption_key)
        await asyncio.to_thread(store.initialize)
        configured_session_store = store
        # Before serving anything: every row stranded by a browser that was simply
        # closed (the `ai_session` cookie has no `max_age`, so its handle is never
        # presented again and `active_session` never gets the chance to delete it
        # lazily) goes now, not whenever someone happens to revisit it (TICK-055 AC5).
        await asyncio.to_thread(store.purge_expired, clock())
        sweep = asyncio.create_task(
            _sweep_expired_sessions(
                store, clock, configured_settings.sweep_interval.total_seconds()
            )
        )
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
            # would silently disable TLS verification for that call too. The model
            # server gets that same verified client: it is an ordinary HTTP service on
            # the app network, with no self-signed cert to accommodate.
            health_openemr_client = httpx.AsyncClient(timeout=2.0, verify=False)
            health_verified_client = httpx.AsyncClient(timeout=2.0)
            owned_http_clients.append(health_openemr_client)
            owned_http_clients.append(health_verified_client)
            configured_health_service = default_health_service(
                HealthSettings.from_environment(configured_settings.issuer),
                health_openemr_client,
                health_verified_client,
            )
        if configured_model_turn_service is None:
            # Two clients, split the same way the services above are: the model server
            # is reached over an ordinary verified connection, while every OpenEMR call
            # hits configured_settings.issuer's host directly and so meets the same
            # untrusted self-signed cert as auth_http_client/chat_openemr_client.
            model_http_client = httpx.AsyncClient(timeout=30.0)
            model_openemr_client = httpx.AsyncClient(timeout=30.0, verify=False)
            owned_http_clients.append(model_http_client)
            owned_http_clients.append(model_openemr_client)
            configured_model_turn_service = _build_model_turn_service(
                model_http_client, model_openemr_client, store, clock
            )
        try:
            yield
        finally:
            # The sweep loop never returns on its own, so without this every test that
            # enters `lifespan_context` would leak a task holding the store open.
            sweep.cancel()
            with suppress(asyncio.CancelledError):
                await sweep
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
        if origin is None or origin.lower() != _chat_origin(configured_settings):
            raise AuthError("request origin is not allowed", 403)
        handle = request.cookies.get(configured_settings.cookie_name)
        now = clock()
        valid = handle is not None and await asyncio.to_thread(
            configured_session_store.active_session, handle, now
        )
        if not valid:
            raise AuthError("an active AI session is required", 401)
        assert handle is not None  # narrows for the type checker; `valid` already required it
        # TICK-055 AC6: the 8-hour TTL stays absolute -- the stored refresh token is
        # never redeemed (evidence/TICK-055/DECISIONS.md records why) -- so the cut has
        # to be announced. Prefixed to whichever reply this turn produces rather than
        # pushed from a new endpoint, so the chat page keeps its single fetch and the
        # patient sees it in the transcript where they are already reading.
        notice = _expiry_notice(
            await asyncio.to_thread(configured_session_store.expires_at, handle, now),
            now,
            configured_settings.expiry_warning_window,
        )
        # Every turn goes to the local model, which decides what happens (TICK-063,
        # LOCAL_LLM_SPEC D9). Nothing is inspected here first: the two `if`s that used to
        # stand at this point -- `onboarding_mode(cursor, turn.message)` and
        # `address_update_mode(...)` -- matched the patient's words against phrasings and
        # steered PHI-bearing turns away from the model. TICK-063 stopped consulting
        # them; TICK-065 deleted the modules behind them, so there is no longer anything
        # here for a later change to reinstate.
        #
        # The fallback is `unavailable_model_turn_service()`, not a deterministic
        # handler, and that is the whole of D12: when the model server is unreachable the
        # chat says so. It does not quietly downgrade to something that can still write.
        service = configured_model_turn_service or unavailable_model_turn_service()
        # Retrieved for this call only (TICK-034 AC1): never persisted, logged, or
        # cached beyond the stream_reply() call they are passed into.
        access_token = await asyncio.to_thread(configured_session_store.access_token, handle, now)
        patient_id = await asyncio.to_thread(configured_session_store.patient_uuid, handle, now)
        return StreamingResponse(
            _with_notice(
                notice,
                service.stream_reply(
                    handle, turn.message, turn.image_base64, access_token, patient_id
                ),
            ),
            media_type="text/plain",
        )

    @server.post("/api/logout")
    async def logout(request: Request) -> Response:
        """End the AI session: delete its row outright and clear the cookie.

        Deliberately not a GET. A GET would be reachable by any off-origin `<img>` or
        redirect with no `Origin` header to check, which is the CSRF sink TICK-055 AC4
        forbids; a POST always carries an `Origin` a browser will not let a page forge.

        Idempotent, and it never reports whether a session existed: a caller presenting
        someone else's guessed handle learns nothing from the response, and a patient
        whose session had already expired still gets their cookie cleared.
        """
        if configured_settings is None or configured_session_store is None:
            raise AuthError("the chat service is unavailable", 503)
        origin = request.headers.get("origin")
        if origin is None or origin.lower() not in _logout_origins(configured_settings):
            raise AuthError("request origin is not allowed", 403)
        handle = request.cookies.get(configured_settings.cookie_name)
        if handle is not None:
            # The row carries the encrypted access and refresh tokens, so this is the
            # step that actually ends the patient's exposure -- not the cookie clear
            # below, which a shared browser or a stale tab could survive.
            await asyncio.to_thread(configured_session_store.delete_session, handle)
            # And the turn path's conversation state: it holds the patient's own words
            # and any change that was read back but not yet saved
            # (`ai_server/app/conversation.py`). Dropping the row while leaving those
            # behind would keep patient data alive past the logout that was supposed to
            # end it. This is now the only such store -- the onboarding and address
            # services kept their own (`_SessionState.identity`, a pending address
            # update) and were discarded here too until TICK-065 deleted them.
            if configured_model_turn_service is not None:
                configured_model_turn_service.discard(handle)
        response = Response(status_code=204)
        # Every attribute /oauth/callback set has to be repeated or the browser treats
        # this as a different cookie and leaves the original in place. Starlette's
        # `delete_cookie` defaults to `samesite="lax"`, which Chrome drops outright
        # when the response arrives in the cross-site context the portal hook creates.
        response.delete_cookie(
            configured_settings.cookie_name,
            path="/",
            httponly=True,
            secure=True,
            samesite="none",
        )
        return response

    @server.get("/oauth/launch")
    async def oauth_launch(request: Request) -> Response:
        """Start a stateful PKCE authorization-code launch, or skip it entirely when the
        patient already has one.

        This is the patient-facing entry point, not a development affordance: it is the
        portal panel's `src` (`PortalChatController::DEFAULT_CHAT_LAUNCH_URL`, overridden
        by `AEAI_PORTAL_CHAT_URL`), so it is hit every time the patient opens the panel.
        Re-running the whole authorization round trip for a patient who is already
        signed in is pure latency, and worse: TICK-045's breakout script sends the login
        page to top level, so a needless launch that OpenEMR decides to prompt on throws
        the patient off the dashboard they are standing on.

        The short-circuit resolves its destination through the same `_panel_or_dashboard`
        the callback uses, deliberately. It is not an exception to FR-31: a live session
        reached at top level gets the dashboard, never the full-page chat (ADR-8).
        """
        if (
            configured_authorization is None
            or configured_settings is None
            or configured_session_store is None
        ):
            raise AuthError("authorization service is unavailable", 503)
        handle = request.cookies.get(configured_settings.cookie_name)
        if handle is not None and await asyncio.to_thread(
            configured_session_store.active_session, handle, clock()
        ):
            return _panel_or_dashboard(configured_settings, request)
        return RedirectResponse(await configured_authorization.launch_url(clock()), status_code=302)

    @server.get("/oauth/callback")
    async def oauth_callback(request: Request) -> Response:
        """Exchange one authorization code and issue only an opaque AI-session cookie.

        Takes the raw query string rather than declaring `code` and `state` as typed
        parameters. FastAPI's signature binding would discard the rest too, but only as
        a side effect of what it happens to bind -- and it would answer a denial, which
        carries no `code`, with a 422 (FR-31, AC3/AC4). The allowlist below is stated in
        the handler so that it is a decision rather than an accident.
        """
        if configured_authorization is None or configured_settings is None:
            raise AuthError("authorization service is unavailable", 503)
        # Everything outside the allowlist is dropped here and never referenced again:
        # a `next=`/`redirect=` parameter added to this URL by anyone cannot reach the
        # destination, which `_panel_or_dashboard` derives from position alone.
        honoured = {
            name: value
            for name, value in request.query_params.items()
            if name in _CALLBACK_HONOURED_PARAMS
        }
        # Read outside the allowlist because it selects an outcome rather than
        # influencing the destination: a denial is answered at the same place a success
        # at this position would be, just without a session. OpenEMR's consent screen
        # (scope-authorize.html.twig) sends `?error=access_denied&error_description=
        # ...&state=...` when the patient declines, which is an ordinary thing for a
        # patient to do -- not a malformed request to 4xx.
        if request.query_params.get("error"):
            return _panel_or_dashboard(configured_settings, request)
        code, state = honoured.get("code"), honoured.get("state")
        if code is None or state is None:
            raise AuthError("authorization response was missing code or state", 400)
        handle = await configured_authorization.callback(code, state, clock())
        response = _panel_or_dashboard(configured_settings, request)
        _set_session_cookie(response, configured_settings, handle)
        return response

    return server


def _build_model_turn_service(
    client: httpx.AsyncClient,
    openemr_client: httpx.AsyncClient,
    session_store: SessionStore,
    clock: Callable[[], datetime],
) -> ModelTurnService:
    """Build the model-first turn service, or a fixed-unavailable one (TICK-063).

    The local model is the front door for every turn now, and D12 accepts the
    consequence: with no deterministic fallback, model-server availability is chat
    availability. So an absent `LLM_MODEL` degrades the whole chat to the honest
    unavailable message rather than failing startup, exactly as an absent `GROQ_API_KEY`
    degraded the old Groq-backed chat service before it.

    `LLM_PROVIDER=groq` degrades the same way, deliberately. Groq may not be the front
    door: it would be an external model receiving what the patient typed, which is the
    boundary violation this whole epic exists to close (D3, FR-34). The turn service's
    `ToolCallClient` Protocol is narrow enough that `HttpGroqClient` cannot satisfy it
    even by accident.

    Groq is nonetheless built here, from its own settings and *not* from
    `selected_llm_provider()` (TICK-064). The two now answer different questions:
    `LLM_PROVIDER` selects the front door, which must be local, while Groq is the
    backing service for exactly one non-writing tool (D13). Wiring it off the provider
    would leave `ask_general_knowledge` permanently unavailable on the only provider the
    chat can actually run on.

    Each service below is optional and independently degradable: a demo missing the
    Portal API base URL loses the tools that need it and keeps the rest of the turn. Note
    what is *not* degradable that way -- an absent `local_client` returns
    `unavailable_model_turn_service()` above rather than a partial turn service, so no
    tool, and in particular no writing tool, is reachable without the model (D12).
    """
    local_client: HttpLocalModelClient | None = None
    if selected_llm_provider() != GROQ:
        try:
            local_client = HttpLocalModelClient(LocalModelSettings.from_environment(), client)
        except LocalModelConfigurationError:
            local_client = None
    if local_client is None:
        return unavailable_model_turn_service()
    general_knowledge = _build_general_knowledge_service(client)
    services = TurnServices(
        general_knowledge=general_knowledge,
        # The pinned local Tesseract engine (TICK-014), never a network call; one
        # shared, in-process service is safe across concurrent sessions since every
        # upload is keyed by its own random upload id.
        ocr=OcrService(SubprocessTesseractEngine()),
    )
    try:
        schedule_settings = OpenEmrScheduleSettings.from_environment()
        booking_tool_settings = BookingToolSettings.from_environment()
        portal_settings = OpenEmrPortalSettings.from_environment()
    except OpenEmrConfigurationError:
        return ModelTurnService(
            client=local_client, services=services, cursors=session_store, clock=clock
        )
    # One slot store shared by discovery (issues tokens) and booking (resolves them),
    # and one appointment store shared by appointment discovery and cancellation, so a
    # token issued in an earlier turn can still be booked/cancelled in a later one, and
    # its own pair rather than a shared one, because these tokens belong to this path's
    # turns alone.
    slot_store = AnonymousSlotStore()
    appointment_store = AnonymousAppointmentStore()
    schedule_adapter = OpenEmrScheduleAdapter(schedule_settings, openemr_client)
    demographics = OpenEmrDemographicsAdapter(portal_settings, openemr_client)
    return ModelTurnService(
        client=local_client,
        services=TurnServices(
            # Same instance as the degraded branch above builds: an absent Portal API
            # base URL must not also cost general knowledge, which needs none of it.
            general_knowledge=general_knowledge,
            slot_discovery=SlotDiscoveryService(
                NoMappedCandidateSource(), schedule_adapter, slot_store
            ),
            appointment_discovery=AppointmentDiscoveryService(schedule_adapter, appointment_store),
            booking=BookingService(
                slot_store, OpenEmrBookingAdapter(portal_settings, openemr_client)
            ),
            cancellation=CancellationService(
                appointment_store, AppointmentCancelAdapter(portal_settings, openemr_client)
            ),
            appointment_request=booking_tool_settings.appointment_request(),
            demographics=demographics,
            onboarding=OnboardingFlow(
                AssessmentDraftAdapter(portal_settings, openemr_client), demographics
            ),
            ocr=services.ocr,
        ),
        cursors=session_store,
        clock=clock,
    )


def _build_general_knowledge_service(
    client: httpx.AsyncClient,
) -> GeneralKnowledgeService | None:
    """Build the one outbound path, or `None` when Groq is not configured.

    `None` is a supported state, not a failure: NFR-20 wants the demo's default path to
    require no paid LLM service, and D13 confines Groq to general-knowledge answers. So
    a deployment with no `GROQ_API_KEY`/`GROQ_ZDR_VERIFIED_ON` loses exactly one
    non-writing tool and keeps every patient-specific capability, all of which are local
    (AC6).

    `PrivacyGate.create()` is built here rather than passed in because this is the only
    remaining caller: the gate exists to screen what leaves, and after TICK-064 this is
    the only thing that leaves.
    """
    try:
        settings = GroqSettings.from_environment()
    except GroqConfigurationError:
        return None
    return GeneralKnowledgeService(PrivacyGate.create(), HttpGroqClient(settings, client))


app = create_app()
