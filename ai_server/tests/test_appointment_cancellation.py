"""Synthetic integration tests for token-resolved appointment cancellation (TICK-036),
mirroring `test_booking.py`'s `BookingService` coverage applied to `CancellationService`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import Appointment
from ai_server.scheduling.appointments import AnonymousAppointmentStore
from ai_server.scheduling.cancel import (
    AppointmentCancelAdapter,
    CancellationService,
    CancelledAppointment,
    StaleAppointmentTokenError,
)

PORTAL_BASE_URL = "https://openemr.test/apis/default"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)


def settings() -> OpenEmrPortalSettings:
    return OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)


def appointment(identifier: str = "real-openemr-id") -> Appointment:
    return Appointment(
        id=identifier,
        status="booked",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=2, minutes=30),
    )


def service_with(
    handler: httpx.MockTransport, store: AnonymousAppointmentStore | None = None
) -> tuple[CancellationService, AnonymousAppointmentStore]:
    active_store = store or AnonymousAppointmentStore()
    client = httpx.AsyncClient(transport=handler)
    adapter = AppointmentCancelAdapter(settings(), client)
    return CancellationService(active_store, adapter), active_store


def run(coroutine):
    return asyncio.run(coroutine)


def test_cancels_the_real_id_the_token_resolved_to_not_any_client_supplied_id() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "real-openemr-id", "status": "cancelled"})

    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(), "patient-a", NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    cancelled = run(service.cancel("token", "patient-a", issued.appointment_token, NOW))

    assert cancelled == CancelledAppointment(id="real-openemr-id", status="cancelled")
    assert str(captured[0].url) == f"{PORTAL_BASE_URL}/portal/patient/appointment/real-openemr-id"
    assert captured[0].headers["authorization"] == "Bearer token"


def test_an_expired_token_fails_before_any_openemr_request_is_made() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR request is permitted for a stale appointment token")

    store = AnonymousAppointmentStore(ttl=timedelta(minutes=15))
    issued = store.issue(appointment(), "patient-a", NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    with pytest.raises(StaleAppointmentTokenError, match="expired"):
        run(
            service.cancel(
                "token", "patient-a", issued.appointment_token, NOW + timedelta(minutes=15)
            )
        )


def test_an_unknown_token_never_reaches_openemr() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR request is permitted for an unknown appointment token")

    service, _ = service_with(httpx.MockTransport(handler))

    with pytest.raises(StaleAppointmentTokenError, match="unknown or already used"):
        run(service.cancel("token", "patient-a", "appt_never-issued", NOW))


def test_a_cross_patient_token_never_reaches_openemr() -> None:
    """Live cross-patient proof at the token layer (mirrors
    `evidence/TICK-028/BINDING_MATRIX.md`'s discipline): a token issued for one
    patient can never be resolved -- let alone cancelled -- by another."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR request is permitted for a cross-patient token")

    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(), "patient-a", NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    with pytest.raises(StaleAppointmentTokenError, match="unknown or already used"):
        run(service.cancel("attacker-token", "patient-b", issued.appointment_token, NOW))


def test_double_submitting_the_same_token_cancels_at_most_once() -> None:
    """Two concurrent cancel attempts sharing one token: only one reaches OpenEMR."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "real-openemr-id", "status": "cancelled"})

    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(), "patient-a", NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    async def scenario() -> list[object]:
        return await asyncio.gather(
            service.cancel("token", "patient-a", issued.appointment_token, NOW),
            service.cancel("token", "patient-a", issued.appointment_token, NOW),
            return_exceptions=True,
        )

    results = run(scenario())

    successes = [r for r in results if isinstance(r, CancelledAppointment)]
    failures = [r for r in results if isinstance(r, StaleAppointmentTokenError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert len(captured) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
