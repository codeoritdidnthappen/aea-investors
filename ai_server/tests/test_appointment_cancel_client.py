"""Synthetic integration tests for `AppointmentCancelAdapter` (TICK-031).

Mirrors `ai_server/tests/test_onboarding_draft_client.py`'s `httpx.MockTransport`
pattern; the real endpoint contract is fixed by
`openemr_modules/aeai-portal-chat/src/Controller/AppointmentCancelController.php` and
`.../src/Service/AppointmentCancelService.php`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError
from ai_server.scheduling.cancel import (
    AppointmentAlreadyCancelledError,
    AppointmentCancelAdapter,
    AppointmentNotFoundError,
    CancelledAppointment,
)

PORTAL_BASE_URL = "https://openemr.test/apis/default"


def settings() -> OpenEmrPortalSettings:
    return OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)


def adapter_with(handler: httpx.MockTransport) -> AppointmentCancelAdapter:
    client = httpx.AsyncClient(transport=handler)
    return AppointmentCancelAdapter(settings(), client)


def run(coroutine):
    return asyncio.run(coroutine)


def test_settings_from_environment_requires_the_portal_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENEMR_PORTAL_BASE_URL", raising=False)
    with pytest.raises(OpenEmrConfigurationError, match="OPENEMR_PORTAL_BASE_URL"):
        OpenEmrPortalSettings.from_environment()


def test_ac2_cancel_puts_to_the_mapped_route_and_returns_the_confirmed_result() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "appt-1", "status": "cancelled"})

    cancelled = run(adapter_with(httpx.MockTransport(handler)).cancel("token", "appt-1"))

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "PUT"
    assert str(request.url) == f"{PORTAL_BASE_URL}/portal/patient/appointment/appt-1"
    assert request.headers["authorization"] == "Bearer token"
    assert cancelled == CancelledAppointment(id="appt-1", status="cancelled")


def test_ac2_a_404_raises_not_found_never_a_success_result() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no appointment with that id for this patient"})

    with pytest.raises(AppointmentNotFoundError):
        run(adapter_with(httpx.MockTransport(handler)).cancel("token", "not-mine"))


def test_ac2_a_409_raises_already_cancelled_with_the_server_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "this appointment is already cancelled"})

    with pytest.raises(AppointmentAlreadyCancelledError, match="already cancelled"):
        run(adapter_with(httpx.MockTransport(handler)).cancel("token", "appt-1"))


def test_an_unexpected_status_raises_a_generic_request_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).cancel("token", "appt-1"))


def test_a_transport_failure_fails_explicitly_with_no_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).cancel("token", "appt-1"))


def test_a_non_json_response_fails_explicitly() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).cancel("token", "appt-1"))


def test_a_200_missing_expected_fields_fails_explicitly() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(OpenEmrRequestError):
        run(adapter_with(httpx.MockTransport(handler)).cancel("token", "appt-1"))
