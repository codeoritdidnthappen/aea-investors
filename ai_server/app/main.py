import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from ai_server.app.address_chat import (
    AddressChatService,
    address_update_mode,
    unavailable_address_service,
)
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
    BookingTool,
    BookingToolSettings,
    ChatService,
    ChatTurnRequest,
    NoMappedCandidateSource,
    ToolFactory,
    no_action_tool_factory,
    unavailable_chat_service,
)
from ai_server.app.health import (
    HealthService,
    HealthSettings,
    default_health_service,
    unavailable_health_service,
)
from ai_server.app.onboarding_chat import (
    OnboardingChatService,
    onboarding_mode,
    unavailable_onboarding_service,
)
from ai_server.llm.groq import GroqConfigurationError, GroqSettings, GroqWorkflow, HttpGroqClient
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
from ai_server.scheduling.reschedule import RescheduleService
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

    Derived from `success_redirect_uri` because that is the URL /oauth/callback sends
    the browser to, so it is the page doing the fetching. TICK-051 splits that setting
    into a separate chat-origin value; this function is the single place both
    `chat_turn` and `logout` read it, so that split repoints both at once instead of
    leaving one behind.
    """
    return _origin_of(settings.success_redirect_uri)


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
    chat_service: ChatService | None = None,
    onboarding_service: OnboardingChatService | None = None,
    address_service: AddressChatService | None = None,
) -> FastAPI:
    """Create the AI server without exposing delegated credentials to the browser."""

    configured_settings = settings
    configured_authorization = authorization
    configured_health_service = health_service
    configured_chat_service = chat_service
    configured_onboarding_service = onboarding_service
    configured_address_service = address_service
    configured_session_store: SessionStore | None = None
    owned_http_clients: list[httpx.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal \
            configured_settings, \
            configured_authorization, \
            configured_health_service, \
            configured_chat_service, \
            configured_onboarding_service, \
            configured_address_service, \
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
            # Same untrusted-self-signed-cert reason as auth_http_client/
            # health_openemr_client above: booking and appointment reads call
            # configured_settings.issuer's host directly, not through the browser.
            chat_openemr_client = httpx.AsyncClient(timeout=30.0, verify=False)
            owned_http_clients.append(chat_openemr_client)
            configured_chat_service = _build_chat_service(
                chat_http_client, chat_openemr_client, clock
            )
        if configured_onboarding_service is None:
            # Same untrusted-self-signed-cert reason as chat_openemr_client above:
            # OnboardingFlow's draft/demographics calls hit configured_settings.issuer's
            # host directly (ai_server/onboarding/draft_client.py never calls an
            # external model, only OpenEMR), and got missed when this client was added.
            onboarding_http_client = httpx.AsyncClient(timeout=30.0, verify=False)
            owned_http_clients.append(onboarding_http_client)
            configured_onboarding_service = _build_onboarding_service(
                onboarding_http_client, store, clock
            )
        if configured_address_service is None:
            # Same untrusted-self-signed-cert reason as onboarding_http_client above:
            # the address write hits configured_settings.issuer's host directly through
            # the same Portal API route, and never calls an external model (TICK-050).
            address_http_client = httpx.AsyncClient(timeout=30.0, verify=False)
            owned_http_clients.append(address_http_client)
            configured_address_service = _build_address_service(address_http_client, store, clock)
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
        cursor = await asyncio.to_thread(configured_session_store.load_cursor, handle, now)
        address = configured_address_service or unavailable_address_service(
            configured_session_store, clock
        )
        if onboarding_mode(cursor, turn.message):
            # Guided onboarding wins the turn and collects the address itself, so any
            # half-finished address update is dropped rather than left to resume later
            # (TICK-050 AC8). It has written nothing, so there is nothing to undo.
            address.discard(handle)
            onboarding = configured_onboarding_service or unavailable_onboarding_service(
                configured_session_store, clock
            )
            return StreamingResponse(
                _with_notice(
                    notice, onboarding.stream_reply(handle, turn.message, turn.image_base64)
                ),
                media_type="text/plain",
            )
        # Ahead of the Groq-backed service below and entirely local, exactly as
        # onboarding is: an address is patient information and must never be sent to an
        # external LLM (NFR-2, TICK-050).
        if address_update_mode(cursor, address.has_pending_update(handle), turn.message):
            return StreamingResponse(
                _with_notice(notice, address.stream_reply(handle, turn.message, turn.image_base64)),
                media_type="text/plain",
            )
        service = configured_chat_service or unavailable_chat_service()
        # Retrieved for this call only (TICK-034 AC1): never persisted, logged, or
        # cached beyond the stream_reply() call they are passed into.
        access_token = await asyncio.to_thread(configured_session_store.access_token, handle, now)
        patient_id = await asyncio.to_thread(configured_session_store.patient_uuid, handle, now)
        return StreamingResponse(
            _with_notice(notice, service.stream_reply(turn.message, access_token, patient_id)),
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
            # Both services keep the patient's own half-finished answers in this
            # process's memory keyed by the handle (`_SessionState.identity`, a pending
            # address update). Dropping the row while leaving those behind would keep
            # patient data alive past the logout that was supposed to end it.
            if configured_onboarding_service is not None:
                configured_onboarding_service.discard(handle)
            if configured_address_service is not None:
                configured_address_service.discard(handle)
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


def _build_chat_service(
    client: httpx.AsyncClient, openemr_client: httpx.AsyncClient, clock: Callable[[], datetime]
) -> ChatService:
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
    tool_factory, slot_discovery, appointment_discovery = _build_scheduling_tool(openemr_client)
    return ChatService(
        workflow=workflow,
        tool_factory=tool_factory,
        slot_discovery=slot_discovery,
        appointment_discovery=appointment_discovery,
        clock=clock,
    )


def _build_scheduling_tool(
    client: httpx.AsyncClient,
) -> tuple[ToolFactory, SlotDiscoveryService | None, AppointmentDiscoveryService | None]:
    """Build the real booking/cancellation tool and slot/appointment discovery, or the
    no-op fallback.

    Mirrors `_build_chat_service`'s/`_build_onboarding_service`'s tolerance of absent
    OpenEMR configuration (TICK-034 AC5, TICK-036): every environment missing the
    FHIR/Portal API base URLs or the admin-configured booking fields keeps today's
    fixed no-action tool and empty `open_slots`/`current_appointments` instead of
    failing startup.
    """
    try:
        schedule_settings = OpenEmrScheduleSettings.from_environment()
        booking_tool_settings = BookingToolSettings.from_environment()
        portal_settings = OpenEmrPortalSettings.from_environment()
    except OpenEmrConfigurationError:
        return no_action_tool_factory(), None, None
    # One slot store shared by discovery (issues tokens) and booking (resolves them),
    # and one appointment store shared by appointment discovery and cancellation, so a
    # token issued in an earlier turn can still be booked/cancelled in a later one.
    slot_store = AnonymousSlotStore()
    appointment_store = AnonymousAppointmentStore()
    # Booking and cancellation both go through the module-added Portal routes now
    # (TICK-040), so they share the one portal_settings instance -- no separate
    # Standard API booking settings needed anymore.
    booking_service = BookingService(slot_store, OpenEmrBookingAdapter(portal_settings, client))
    cancellation_service = CancellationService(
        appointment_store, AppointmentCancelAdapter(portal_settings, client)
    )
    # Composes the same booking_service/cancellation_service instances above, not new
    # ones, so a reschedule's book/cancel calls still resolve through the one shared
    # slot_store/appointment_store single-use guarantee (TICK-020).
    reschedule_service = RescheduleService(booking_service, cancellation_service)
    schedule_adapter = OpenEmrScheduleAdapter(schedule_settings, client)
    slot_discovery = SlotDiscoveryService(NoMappedCandidateSource(), schedule_adapter, slot_store)
    appointment_discovery = AppointmentDiscoveryService(schedule_adapter, appointment_store)
    appointment_request = booking_tool_settings.appointment_request()

    def factory(access_token: str | None, patient_id: str | None, now: datetime) -> BookingTool:
        return BookingTool(
            booking=booking_service,
            cancellation=cancellation_service,
            reschedule=reschedule_service,
            appointment_request=appointment_request,
            access_token=access_token,
            patient_id=patient_id,
            now=now,
        )

    return factory, slot_discovery, appointment_discovery


def _build_onboarding_service(
    client: httpx.AsyncClient, session_store: SessionStore, clock: Callable[[], datetime]
) -> OnboardingChatService:
    """Build the real OpenEMR-backed onboarding service, or a fixed-unavailable
    fallback -- mirrors `_build_chat_service`'s tolerance of absent configuration, so a
    demo missing the Portal API base URL degrades onboarding instead of failing
    startup.
    """
    try:
        portal_settings = OpenEmrPortalSettings.from_environment()
    except OpenEmrConfigurationError:
        return unavailable_onboarding_service(session_store, clock)
    # Draft and demographics both go through module-added Portal routes (TICK-042),
    # so they share the one portal_settings instance -- no separate Standard API
    # demographics settings needed anymore.
    flow = OnboardingFlow(
        AssessmentDraftAdapter(portal_settings, client),
        OpenEmrDemographicsAdapter(portal_settings, client),
    )
    # The pinned local Tesseract engine (TICK-014), never a network call; one shared,
    # in-process OcrService is safe across concurrent sessions since every upload is
    # keyed by its own random upload id (TICK-044 wires this previously-unreferenced
    # service into the chat turn for the first time).
    ocr = OcrService(SubprocessTesseractEngine())
    return OnboardingChatService(flow=flow, session_store=session_store, ocr=ocr, clock=clock)


def _build_address_service(
    client: httpx.AsyncClient, session_store: SessionStore, clock: Callable[[], datetime]
) -> AddressChatService:
    """Build the real OpenEMR-backed address-update service, or a fixed-unavailable
    fallback -- mirrors `_build_onboarding_service`'s tolerance of absent configuration,
    so a demo missing the Portal API base URL degrades this flow instead of failing
    startup.

    Reuses the same `OpenEmrDemographicsAdapter` type onboarding writes through
    (TICK-042/TICK-049), so there is one demographics write path, not two.
    """
    try:
        portal_settings = OpenEmrPortalSettings.from_environment()
    except OpenEmrConfigurationError:
        return unavailable_address_service(session_store, clock)
    return AddressChatService(
        demographics=OpenEmrDemographicsAdapter(portal_settings, client),
        session_store=session_store,
        clock=clock,
    )


app = create_app()
