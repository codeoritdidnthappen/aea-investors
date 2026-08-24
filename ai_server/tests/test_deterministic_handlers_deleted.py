"""Tests for TICK-065: the deterministic handlers are gone, and the outage is honest.

`docs/LOCAL_LLM_SPEC.md` D12. `ai_server/app/address_chat.py` and
`ai_server/app/onboarding_chat.py` matched the patient's words against phrase lists and
parsed fields out of free text. TICK-063 stopped consulting them; this suite asserts they
are deleted, that nothing imports them, and that the consequence D12 accepted is handled
rather than merely accepted -- an unreachable model server produces an honest message, a
visible `/health` dependency, and no write.

The last point is the one worth being careful about. "The chat says it is unavailable" is
cheap to satisfy and easy to satisfy wrongly, by saying it *after* something has already
been attempted. So the write assertions here are made against a recording OpenEMR
transport: what they check is that OpenEMR received nothing at all.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from ai_server.app.auth import AuthSettings, OAuthTokens, SessionStore
from ai_server.app.chat import ASSISTANT_UNAVAILABLE_RESPONSE
from ai_server.app.health import (
    DEPENDENCY_NAMES,
    HealthSettings,
    default_health_service,
)
from ai_server.app.main import create_app
from ai_server.app.model_turn import ModelTurnService, TurnServices
from ai_server.llm.local import HttpLocalModelClient, LocalModelSettings
from ai_server.onboarding.draft_client import AssessmentDraftAdapter, OpenEmrPortalSettings
from ai_server.onboarding.flow import OnboardingFlow
from ai_server.openemr.demographics import OpenEmrDemographicsAdapter

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
MODEL_BASE_URL = "http://ollama.test:11434"
PORTAL_BASE_URL = "https://openemr.test/apis/default"

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The two modules D12 deletes, and the mode-detection and text-parsing functions they
# owned. Named individually rather than checked as "the file is gone", because the
# failure this guards against is the file being deleted while its mechanism is copied
# somewhere else under the same name.
DELETED_MODULES = ("ai_server.app.address_chat", "ai_server.app.onboarding_chat")
DELETED_SYMBOLS = frozenset(
    {
        # The mode detection that routed to them, from `main.py`'s two `if`s.
        "onboarding_mode",
        "address_update_mode",
        # The phrase matching behind that routing.
        "is_onboarding_start_request",
        "is_address_update_request",
        "is_confirmation",
        "is_cancellation",
        # The free-text field extraction -- `_parse_freeform_address` is the function
        # that wrote "Update it to: 2002 Bridge Avenue" into a chart (LOCAL_LLM_SPEC
        # "Why"), and the reason this epic exists.
        "parse_address_reply",
        "_parse_freeform_address",
        "_looks_like_an_address_attempt",
        # Helpers owned by the deleted flows and used by nothing else.
        "_hinted_prompt",
        "_next_field",
        "_review_summary",
        "_parsed_upload_request",
    }
)


def _source_files() -> Iterator[Path]:
    """Every Python file this repo ships, tests and scripts included."""
    for directory in ("ai_server", "scripts", "eval"):
        yield from sorted((_REPO_ROOT / directory).rglob("*.py"))


# --- AC1: both modules are deleted, and nothing reaches for them --------------------


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_ac1_the_deterministic_handler_modules_no_longer_exist(module: str) -> None:
    assert importlib.util.find_spec(module) is None, f"{module} is still importable"
    assert not (_REPO_ROOT / (module.replace(".", "/") + ".py")).exists()


def test_ac1_no_module_imports_a_deleted_handler() -> None:
    """Parsed rather than grepped, so a comment or docstring that *names* one of these
    modules -- several deliberately do, to explain why it is gone -- cannot be mistaken
    for an import of it."""
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for name in imported:
                if any(
                    name == module or name.startswith(module + ".") for module in DELETED_MODULES
                ):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno} imports {name}")
    assert offenders == []


def test_ac1_no_module_defines_a_deleted_mode_detection_or_parsing_symbol() -> None:
    """The functions go with the modules. A file that deleted `address_chat.py` and
    reimplemented `_parse_freeform_address` elsewhere would satisfy the file check above
    and defeat the entire point of the ticket."""
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in DELETED_SYMBOLS:
                    offenders.append(
                        f"{path.relative_to(_REPO_ROOT)}:{node.lineno} defines {node.name}"
                    )
    assert offenders == []


def test_ac1_the_app_cannot_be_given_a_deterministic_handler_at_all() -> None:
    """`create_app`'s signature is the enforcement: reinstating a phrase-matched bypass
    of the model would have to add a parameter back here, in the diff, rather than being
    wired up quietly inside the lifespan."""
    parameters = set(inspect.signature(create_app).parameters)

    assert "onboarding_service" not in parameters
    assert "address_service" not in parameters
    assert "model_turn_service" in parameters


# --- AC2: the one surviving intent-shaped pattern is out of the turn path -----------


def test_ac2_the_surviving_distress_corpus_is_not_reached_from_the_turn_path() -> None:
    """`ai_server/onboarding/triggers.py` holds the last intent-shaped pattern match in
    the codebase: `detect_distress`, a substring corpus over approved phrases. AC2 allows
    it to survive only if it is named with a reason rather than left silently, and both
    `triggers.py`'s and `model_turn.py`'s module docstrings name it.

    This pins the factual half of that note -- that nothing under `ai_server/app/` reaches
    it, so no turn passes through it. `evidence/TICK-067/FOLLOW_UP_TICKETS.md` owns
    running it on every turn; when that lands, this test failing is the reminder that both
    docstrings now describe something that is no longer true.
    """
    callers: list[str] = []
    for path in sorted((_REPO_ROOT / "ai_server" / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ai_server.onboarding.triggers":
                callers.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")

    assert callers == []


# --- AC3: the outage is reported honestly -------------------------------------------


def test_ac3_the_unavailable_message_is_plain_and_says_the_portal_still_works() -> None:
    text = ASSISTANT_UNAVAILABLE_RESPONSE

    # Temporary, not broken and not permanent.
    assert "temporarily unavailable" in text
    # And the portal still works, said outright rather than left to be inferred -- with
    # no fallback path it is the only thing the patient can still do.
    assert "Your patient portal is still working normally." in text


def test_ac3_the_unavailable_message_names_no_internal_component() -> None:
    """A patient can act on "the assistant is unavailable". They can do nothing with the
    name of the process that is down, and a deployment detail in a patient-facing string
    is a small information leak besides."""
    lowered = ASSISTANT_UNAVAILABLE_RESPONSE.lower()
    for internal in (
        "model server",
        "ollama",
        "vllm",
        "groq",
        "llm",
        "fastapi",
        "ai server",
        "modelturnservice",
        "localhost",
        "11434",
        "error",
        "exception",
        "timeout",
        "connection",
    ):
        assert internal not in lowered, f"{internal!r} is an internal detail"


def test_ac3_an_unreachable_model_server_answers_the_patient_at_the_route(
    tmp_path: Path,
) -> None:
    """End to end through `POST /api/chat`, with a model transport that refuses the
    connection the way a stopped server does."""
    configured = _settings(tmp_path)
    cookie = _active_session_cookie(configured)
    service, portal = _turn_service(_refusing_model_client())
    app = create_app(configured, clock=lambda: NOW, model_turn_service=service)

    response = asyncio.run(_post_chat(app, cookie, "I need to change my address."))

    assert response.status_code == 200
    assert response.text == ASSISTANT_UNAVAILABLE_RESPONSE
    assert portal.requests == []


# --- AC4: `/health` makes the outage visible before a patient finds it ---------------


def test_ac4_health_reports_model_server_reachability_alongside_its_existing_deps() -> None:
    assert DEPENDENCY_NAMES == (
        "ai_server",
        "model_server",
        "openemr_api",
        "ocr",
        "external_llm",
    )


def test_ac4_health_reports_the_model_server_down_when_it_is_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    report = asyncio.run(_health_report(_local_settings(), refuse))

    assert report["dependencies"]["model_server"] == "unavailable"
    assert report["status"] == "degraded"


def test_ac4_health_reports_the_model_server_up_without_running_an_inference() -> None:
    """Reachability is a GET on the OpenAI-compatible model listing, which Ollama and
    vLLM both answer (D7). A probe that ran a completion would put load on the model
    server on every monitoring poll, and would report a model that is merely slow to
    generate as an outage."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "llama3.1:8b-instruct-q4_K_M"}]})

    report = asyncio.run(_health_report(_local_settings(), answer))

    assert report["dependencies"]["model_server"] == "ok"
    # One GET on the listing, and nothing at all on the endpoint a turn generates
    # through. (`seen` also holds the OpenEMR probe's own request, which shares this
    # transport, so it is filtered by host rather than counted.)
    to_model = [request for request in seen if request.url.host == "ollama.test"]
    assert [(request.method, str(request.url)) for request in to_model] == [
        ("GET", f"{MODEL_BASE_URL}/v1/models")
    ]


def test_ac4_an_unconfigured_model_server_is_reported_unavailable_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LLM_PROVIDER=groq` and an absent `LLM_MODEL` are the two states in which
    `_build_model_turn_service` hands back `unavailable_model_turn_service()`. From the
    patient's side that is the same outage as an unreachable server, so `/health` must
    not report `ok` while every turn answers "temporarily unavailable"."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b-instruct-q4_K_M")
    assert HealthSettings.from_environment("https://openemr.test").model_server is None

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert HealthSettings.from_environment("https://openemr.test").model_server is None

    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b-instruct-q4_K_M")
    monkeypatch.setenv("OLLAMA_HOST", MODEL_BASE_URL)
    configured = HealthSettings.from_environment("https://openemr.test").model_server
    assert configured is not None
    assert configured.base_url == MODEL_BASE_URL


# --- AC5: no write can execute while the model is unavailable -----------------------


def test_ac5_a_turn_that_would_write_reaches_openemr_with_nothing(tmp_path: Path) -> None:
    """Failing closed on writes is the point. The assertion is not that the reply was an
    apology -- it is that OpenEMR was never called."""
    del tmp_path
    service, portal = _turn_service(_refusing_model_client())

    reply = _turn(service, "Change my address to 88 Larch Street, Toms River NJ 08753.")

    assert reply == ASSISTANT_UNAVAILABLE_RESPONSE
    assert portal.requests == []


def test_ac5_a_write_already_read_back_is_not_saved_once_the_model_goes_down() -> None:
    """The sharper case. A change validated and read back last turn is sitting in
    conversation state with the patient's "yes" arriving now -- the one moment at which a
    write could plausibly be executed without asking the model anything.

    It is not, and it must not be: recognising a confirmation is the model's job since
    TICK-063 (there is no keyword list left to fall back to), and executing a pending
    write on an unrecognised turn would be exactly the bad-parse-reaches-a-chart failure
    D12 refuses to keep a path open for.
    """
    proposal = json.dumps(
        {
            "tool": "update_address",
            "arguments": {
                "street1": "88 Larch Street",
                "city": "Toms River",
                "state": "NJ",
                "zip_code": "08753",
            },
        }
    )
    service, portal = _turn_service(_scripted_model_client([proposal]))

    read_back = _turn(service, "Change my address to 88 Larch Street, Toms River NJ 08753.")

    assert "88 Larch Street" in read_back
    assert service.conversations.state("handle-1", NOW).pending is not None
    assert portal.requests == [], "nothing may be written before the patient confirms"

    # The model server goes down between the read-back and the confirmation.
    service.client = _refusing_model_client()

    assert _turn(service, "yes") == ASSISTANT_UNAVAILABLE_RESPONSE
    assert portal.requests == []


# --- Fixtures -----------------------------------------------------------------------


@dataclass
class _RecordingPortal:
    """A mocked OpenEMR that records every request and answers the writes successfully.

    Answering successfully is deliberate: if a write were attempted it would *land*, so
    an empty `requests` list is a claim about this system's behaviour rather than an
    artefact of the double refusing.
    """

    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})


def _local_settings() -> LocalModelSettings:
    return LocalModelSettings(model="llama3.1:8b-instruct-q4_K_M", base_url=MODEL_BASE_URL)


def _refusing_model_client() -> HttpLocalModelClient:
    """A model client pointed at a server that is not listening."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return HttpLocalModelClient(
        _local_settings(), httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )


def _scripted_model_client(replies: list[str]) -> HttpLocalModelClient:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        if not replies:
            raise AssertionError("the model was asked for more turns than were scripted")
        return httpx.Response(200, json={"choices": [{"message": {"content": replies.pop(0)}}]})

    return HttpLocalModelClient(
        _local_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _turn_service(
    client: HttpLocalModelClient,
) -> tuple[ModelTurnService, _RecordingPortal]:
    """A real turn service whose every writing tool is wired to a recording OpenEMR."""
    portal = _RecordingPortal()
    portal_client = httpx.AsyncClient(transport=httpx.MockTransport(portal.handler))
    portal_settings = OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)
    demographics = OpenEmrDemographicsAdapter(portal_settings, portal_client)
    return (
        ModelTurnService(
            client=client,
            services=TurnServices(
                demographics=demographics,
                onboarding=OnboardingFlow(
                    AssessmentDraftAdapter(portal_settings, portal_client), demographics
                ),
            ),
            clock=lambda: NOW,
            metrics=lambda _: None,
        ),
        portal,
    )


def _turn(service: ModelTurnService, message: str, handle: str = "handle-1") -> str:
    async def scenario() -> str:
        return "".join(
            [
                chunk
                async for chunk in service.stream_reply(
                    handle, message, access_token="synthetic-access", patient_id="patient-uuid"
                )
            ]
        )

    return asyncio.run(scenario())


async def _health_report(model_server: LocalModelSettings, handler: Any) -> dict[str, object]:
    settings = HealthSettings(
        openemr_url="https://openemr.test", groq_api_key=None, model_server=model_server
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await default_health_service(settings, client, client).report()


def _settings(tmp_path: Path) -> AuthSettings:
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
        expiry_warning_window=timedelta(0),
    )


def _active_session_cookie(configured: AuthSettings) -> str:
    store = SessionStore(configured.database_path, configured.encryption_key)
    store.initialize()
    tokens = OAuthTokens("synthetic-access", "synthetic-refresh", "synthetic-nonce")
    return store.create_session(tokens, NOW, configured.session_ttl)


async def _post_chat(app: Any, cookie: str, message: str) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://chat.test",
            cookies={"ai_session": cookie},
        ) as client:
            return await client.post(
                "/api/chat",
                json={"message": message},
                headers={"Origin": "https://chat.test"},
            )
