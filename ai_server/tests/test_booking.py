"""Synthetic integration tests for slot-token booking (TICK-031)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError
from ai_server.scheduling.booking import (
    AppointmentRequest,
    BookedAppointment,
    BookingService,
    OpenEmrBookingAdapter,
    OpenEmrBookingSettings,
    SlotBookingError,
)
from ai_server.scheduling.slots import AnonymousSlotStore, CandidateSlot

API_BASE_URL = "https://openemr.test/apis/default/api"
TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)

REQUEST = AppointmentRequest(
    category_id="5", title="Office Visit", facility_id="9", billing_location_id="10"
)


def settings() -> OpenEmrBookingSettings:
    return OpenEmrBookingSettings(api_base_url=API_BASE_URL)


def adapter_with(handler: httpx.MockTransport) -> OpenEmrBookingAdapter:
    client = httpx.AsyncClient(transport=handler)
    return OpenEmrBookingAdapter(settings(), client)


def candidate(hours_from_now: float = 2, duration_minutes: int = 30) -> CandidateSlot:
    start = NOW + timedelta(hours=hours_from_now)
    return CandidateSlot(starts_at=start, ends_at=start + timedelta(minutes=duration_minutes))


def run(coroutine):
    return asyncio.run(coroutine)


def test_settings_from_environment_requires_the_api_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENEMR_API_BASE_URL", raising=False)
    with pytest.raises(OpenEmrConfigurationError, match="OPENEMR_API_BASE_URL"):
        OpenEmrBookingSettings.from_environment()

    monkeypatch.setenv("OPENEMR_API_BASE_URL", f"{API_BASE_URL}/")
    assert OpenEmrBookingSettings.from_environment().api_base_url == API_BASE_URL


def test_ac1_creates_an_appointment_at_the_exact_resolved_window_only() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "42"})

    starts_at = NOW + timedelta(hours=2)
    ends_at = starts_at + timedelta(minutes=30)

    identifier = run(
        adapter_with(httpx.MockTransport(handler)).create_appointment(
            "synthetic-access-token",
            "7",
            starts_at=starts_at,
            ends_at=ends_at,
            request=REQUEST,
        )
    )

    assert identifier == "42"
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == f"{API_BASE_URL}/patient/7/appointment"
    assert request.headers["authorization"] == "Bearer synthetic-access-token"
    body = json.loads(request.content)
    assert body == {
        "pc_catid": "5",
        "pc_title": "Office Visit",
        "pc_duration": 1800,
        "pc_hometext": "Booked by the AI scheduling assistant",
        "pc_apptstatus": "-",
        "pc_eventDate": starts_at.date().isoformat(),
        "pc_startTime": starts_at.strftime("%H:%M"),
        "pc_facility": "9",
        "pc_billing_location": "10",
    }


def test_ac1_a_provider_id_is_included_only_when_supplied() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "1"})

    with_provider = AppointmentRequest(
        category_id="5",
        title="Office Visit",
        facility_id="9",
        billing_location_id="10",
        provider_id="3",
    )
    starts_at = NOW + timedelta(hours=2)
    run(
        adapter_with(httpx.MockTransport(handler)).create_appointment(
            "token",
            "7",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            request=with_provider,
        )
    )

    assert json.loads(captured[0].content)["pc_aid"] == "3"


def test_ac3_a_non_200_response_fails_explicitly_with_no_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "conflict"})

    starts_at = NOW + timedelta(hours=2)

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).create_appointment(
            "token",
            "7",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            request=REQUEST,
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_ac3_a_non_json_response_fails_explicitly() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    starts_at = NOW + timedelta(hours=2)

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).create_appointment(
            "token",
            "7",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            request=REQUEST,
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_ac3_a_transport_failure_fails_explicitly_with_no_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    starts_at = NOW + timedelta(hours=2)

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).create_appointment(
            "token",
            "7",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            request=REQUEST,
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


# --- BookingService: resolves a slot token before ever touching OpenEMR ------------


def service_with(
    handler: httpx.MockTransport, store: AnonymousSlotStore | None = None
) -> tuple[BookingService, AnonymousSlotStore]:
    active_store = store or AnonymousSlotStore()
    client = httpx.AsyncClient(transport=handler)
    adapter = OpenEmrBookingAdapter(settings(), client)
    return BookingService(active_store, adapter), active_store


def test_ac1_books_the_real_window_the_token_resolved_to_not_any_client_supplied_time() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "99"})

    store = AnonymousSlotStore()
    slot = candidate(3)
    issued = store.issue(slot, NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    booked = run(service.book("token", "7", issued.slot_token, REQUEST, NOW))

    assert booked == BookedAppointment(id="99", starts_at=slot.starts_at, ends_at=slot.ends_at)
    body = json.loads(captured[0].content)
    assert body["pc_eventDate"] == slot.starts_at.date().isoformat()
    assert body["pc_startTime"] == slot.starts_at.strftime("%H:%M")


def test_ac3_an_expired_token_fails_before_any_openemr_request_is_made() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR request is permitted for a stale slot token")

    store = AnonymousSlotStore(ttl=timedelta(minutes=15))
    issued = store.issue(candidate(1), NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    with pytest.raises(SlotBookingError, match="expired"):
        run(service.book("token", "7", issued.slot_token, REQUEST, NOW + timedelta(minutes=15)))


def test_ac4_double_submitting_the_same_token_books_at_most_once() -> None:
    """Two concurrent booking attempts sharing one token: only one reaches OpenEMR."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "1"})

    store = AnonymousSlotStore()
    issued = store.issue(candidate(2), NOW)
    service, _ = service_with(httpx.MockTransport(handler), store)

    async def scenario() -> list[object]:
        return await asyncio.gather(
            service.book("token", "7", issued.slot_token, REQUEST, NOW),
            service.book("token", "7", issued.slot_token, REQUEST, NOW),
            return_exceptions=True,
        )

    results = run(scenario())

    successes = [r for r in results if isinstance(r, BookedAppointment)]
    failures = [r for r in results if isinstance(r, SlotBookingError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert len(captured) == 1


def test_ac4_an_unknown_token_never_reaches_openemr() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no OpenEMR request is permitted for an unknown slot token")

    service, _ = service_with(httpx.MockTransport(handler))

    with pytest.raises(SlotBookingError, match="unknown or already used"):
        run(service.book("token", "7", "slot_never-issued", REQUEST, NOW))
