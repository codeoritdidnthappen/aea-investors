"""Synthetic integration tests for `AssessmentDraftAdapter` (TICK-017).

Mirrors `ai_server/tests/test_openemr_demographics.py`'s `httpx.MockTransport`
pattern; the real endpoint contract is fixed by
`openemr_modules/aeai-portal-chat/src/Controller/AssessmentDraftController.php` and
`.../src/Service/AssessmentDraftService.php`, proven live in
`evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md`.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_server.onboarding.draft_client import (
    AssessmentDraft,
    AssessmentDraftAdapter,
    AssessmentDraftConflictError,
    AssessmentDraftNotFoundError,
    AssessmentDraftValidationError,
    OpenEmrPortalSettings,
)
from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError

PORTAL_BASE_URL = "https://openemr.test/apis/default"


def settings() -> OpenEmrPortalSettings:
    return OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)


def adapter_with(handler: httpx.MockTransport) -> AssessmentDraftAdapter:
    client = httpx.AsyncClient(transport=handler)
    return AssessmentDraftAdapter(settings(), client)


def run(coroutine):
    return asyncio.run(coroutine)


def test_settings_from_environment_requires_the_portal_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENEMR_PORTAL_BASE_URL", raising=False)
    with pytest.raises(OpenEmrConfigurationError, match="OPENEMR_PORTAL_BASE_URL"):
        OpenEmrPortalSettings.from_environment()

    monkeypatch.setenv("OPENEMR_PORTAL_BASE_URL", f"{PORTAL_BASE_URL}/")
    assert OpenEmrPortalSettings.from_environment().portal_base_url == PORTAL_BASE_URL


def test_ac2_create_posts_to_the_mapped_route_and_returns_the_new_draft() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201, json={"uuid": "draft-1", "status": "draft", "fields": {"help_type": "both"}}
        )

    draft = run(adapter_with(httpx.MockTransport(handler)).create("token", {"help_type": "both"}))

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == f"{PORTAL_BASE_URL}/portal/patient/assessment"
    assert request.headers["authorization"] == "Bearer token"
    assert json.loads(request.content) == {"help_type": "both"}
    assert draft == AssessmentDraft(uuid="draft-1", status="draft", fields={"help_type": "both"})


def test_ac2_read_gets_the_draft_by_id() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"uuid": "draft-1", "status": "draft", "fields": {}})

    draft = run(adapter_with(httpx.MockTransport(handler)).read("token", "draft-1"))

    assert captured[0].method == "GET"
    assert str(captured[0].url) == f"{PORTAL_BASE_URL}/portal/patient/assessment/draft-1"
    assert draft.uuid == "draft-1"


def test_ac2_a_second_reader_of_the_same_draft_after_restart_sees_the_checkpointed_fields() -> None:
    """AC2: draft changes checkpoint through OpenEMR and reload after a restart.

    Two independent adapter instances (standing in for two AI-server process
    lifetimes) share nothing but the mocked OpenEMR draft store; the second
    instance's `read()` still sees what the first instance's `update()` wrote.
    """
    store: dict[str, object] = {"status": "draft", "fields": {}}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            store["fields"] = {**store["fields"], **json.loads(request.content)}
            return httpx.Response(
                200, json={"uuid": "draft-1", "status": store["status"], "fields": store["fields"]}
            )
        return httpx.Response(
            200, json={"uuid": "draft-1", "status": store["status"], "fields": store["fields"]}
        )

    transport = httpx.MockTransport(handler)
    first_process_adapter = adapter_with(transport)
    run(first_process_adapter.update("token", "draft-1", {"help_type": "counseling_or_therapy"}))

    second_process_adapter = adapter_with(transport)
    reloaded = run(second_process_adapter.read("token", "draft-1"))

    assert reloaded.fields == {"help_type": "counseling_or_therapy"}


def test_ac2_update_puts_only_the_new_fields_and_returns_the_merged_draft() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"uuid": "draft-1", "status": "draft", "fields": body})

    draft = run(
        adapter_with(httpx.MockTransport(handler)).update("token", "draft-1", {"help_type": "both"})
    )

    assert draft.fields == {"help_type": "both"}


def test_ac3_update_with_complete_sends_status_completed() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"uuid": "draft-1", "status": "completed", "fields": {}})

    draft = run(
        adapter_with(httpx.MockTransport(handler)).update("token", "draft-1", {}, complete=True)
    )

    assert json.loads(captured[0].content) == {"status": "completed"}
    assert draft.status == "completed"


def test_a_404_raises_not_found() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no assessment draft with that id"})

    with pytest.raises(AssessmentDraftNotFoundError):
        run(adapter_with(httpx.MockTransport(handler)).read("token", "unknown"))


def test_a_409_raises_conflict_with_the_server_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "this assessment is already completed"})

    with pytest.raises(AssessmentDraftConflictError, match="already completed"):
        run(
            adapter_with(httpx.MockTransport(handler)).update(
                "token", "draft-1", {"help_type": "both"}
            )
        )


def test_a_400_raises_validation_error_with_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "validation failed",
                "details": ["help_type must be one of: counseling_or_therapy, ..."],
            },
        )

    with pytest.raises(AssessmentDraftValidationError) as excinfo:
        run(
            adapter_with(httpx.MockTransport(handler)).update(
                "token", "draft-1", {"help_type": "invalid"}
            )
        )
    assert excinfo.value.details == ["help_type must be one of: counseling_or_therapy, ..."]


def test_an_unexpected_status_raises_a_generic_request_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).read("token", "draft-1"))


def test_a_transport_failure_fails_explicitly_with_no_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).read("token", "draft-1"))


def test_a_non_json_response_fails_explicitly() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).read("token", "draft-1"))
