"""Synthetic integration tests for the read-only OpenEMR scheduling adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_server.openemr.adapter import (
    Appointment,
    OpenEmrConfigurationError,
    OpenEmrOperationNotMappedError,
    OpenEmrRequestError,
    OpenEmrScheduleAdapter,
    OpenEmrScheduleSettings,
)

FHIR_BASE_URL = "https://openemr.test/apis/default/fhir"


def settings() -> OpenEmrScheduleSettings:
    return OpenEmrScheduleSettings(fhir_base_url=FHIR_BASE_URL)


def bundle(*resources: dict[str, object]) -> dict[str, object]:
    return {
        "resourceType": "Bundle",
        "entry": [{"resource": resource} for resource in resources],
    }


def appointment_resource(
    identifier: str,
    status: str,
    start: str,
    end: str,
    description: str | None = None,
) -> dict[str, object]:
    resource: dict[str, object] = {
        "resourceType": "Appointment",
        "id": identifier,
        "status": status,
        "start": start,
        "end": end,
    }
    if description is not None:
        resource["description"] = description
    return resource


def adapter_with(handler: httpx.MockTransport) -> OpenEmrScheduleAdapter:
    client = httpx.AsyncClient(transport=handler)
    return OpenEmrScheduleAdapter(settings(), client)


def run(coroutine):
    return asyncio.run(coroutine)


def test_settings_from_environment_requires_the_fhir_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENEMR_FHIR_BASE_URL", raising=False)
    with pytest.raises(OpenEmrConfigurationError, match="OPENEMR_FHIR_BASE_URL"):
        OpenEmrScheduleSettings.from_environment()

    monkeypatch.setenv("OPENEMR_FHIR_BASE_URL", f"{FHIR_BASE_URL}/")
    assert OpenEmrScheduleSettings.from_environment().fhir_base_url == FHIR_BASE_URL


def test_ac1_reads_appointments_only_through_the_mapped_fhir_endpoint() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=bundle(
                appointment_resource(
                    "appt-1", "booked", "2026-08-25T13:00:00-05:00", "2026-08-25T13:30:00-05:00"
                )
            ),
        )

    async def scenario() -> list[Appointment]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenEmrScheduleAdapter(settings(), client).active_appointments(
                "synthetic-access-token"
            )

    appointments = run(scenario())

    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert str(captured[0].url) == f"{FHIR_BASE_URL}/Appointment"
    assert captured[0].headers["authorization"] == "Bearer synthetic-access-token"
    assert appointments == [
        Appointment(
            id="appt-1",
            status="booked",
            start=datetime(2026, 8, 25, 13, 0, tzinfo=timezone(timedelta(hours=-5))),
            end=datetime(2026, 8, 25, 13, 30, tzinfo=timezone(timedelta(hours=-5))),
            description=None,
        )
    ]


@pytest.mark.parametrize("operation", ["availability", "office_hours", "closures"])
def test_ac1_unmapped_operations_fail_explicitly_with_no_request_and_no_fallback(
    operation: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request is permitted for an unmapped operation")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenEmrScheduleAdapter(settings(), client)
            await getattr(adapter, operation)()

    with pytest.raises(OpenEmrOperationNotMappedError):
        run(scenario())


def test_ac1_a_non_200_response_fails_explicitly_with_no_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await OpenEmrScheduleAdapter(settings(), client).active_appointments("expired-token")

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_ac1_a_malformed_bundle_fails_explicitly_with_no_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resourceType": "OperationOutcome"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await OpenEmrScheduleAdapter(settings(), client).active_appointments("token")

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_ac2_appointment_times_carry_the_offset_openemr_returned() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=bundle(
                appointment_resource(
                    "appt-utc", "booked", "2026-08-25T18:00:00Z", "2026-08-25T18:30:00Z"
                )
            ),
        )

    appointments = run(adapter_with(httpx.MockTransport(handler)).active_appointments("token"))

    assert appointments[0].start == datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)
    assert appointments[0].start.tzinfo is not None


def test_ac2_same_local_wall_clock_resolves_to_different_utc_across_dst() -> None:
    """Chicago is UTC-5 (CDT) in summer and UTC-6 (CST) in winter for the same 13:00 wall clock."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=bundle(
                appointment_resource(
                    "appt-summer",
                    "booked",
                    "2026-06-15T13:00:00-05:00",
                    "2026-06-15T13:30:00-05:00",
                ),
                appointment_resource(
                    "appt-winter",
                    "booked",
                    "2026-12-15T13:00:00-06:00",
                    "2026-12-15T13:30:00-06:00",
                ),
            ),
        )

    summer, winter = run(adapter_with(httpx.MockTransport(handler)).active_appointments("token"))

    assert summer.start.utcoffset() == timedelta(hours=-5)
    assert winter.start.utcoffset() == timedelta(hours=-6)
    assert summer.start.astimezone(timezone.utc) == datetime(
        2026, 6, 15, 18, 0, tzinfo=timezone.utc
    )
    assert winter.start.astimezone(timezone.utc) == datetime(
        2026, 12, 15, 19, 0, tzinfo=timezone.utc
    )


def test_ac2_a_naive_appointment_time_fails_explicitly() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=bundle(
                appointment_resource(
                    "appt-naive", "booked", "2026-08-25T13:00:00", "2026-08-25T13:30:00"
                )
            ),
        )

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).active_appointments("token")

    with pytest.raises(OpenEmrRequestError, match="timezone"):
        run(scenario())


def test_ac3_cancelled_appointments_are_omitted_but_only_read_never_deleted() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=bundle(
                appointment_resource(
                    "appt-active",
                    "booked",
                    "2026-08-25T13:00:00-05:00",
                    "2026-08-25T13:30:00-05:00",
                ),
                appointment_resource(
                    "appt-cancelled",
                    "cancelled",
                    "2026-08-20T09:00:00-05:00",
                    "2026-08-20T09:30:00-05:00",
                ),
            ),
        )

    appointments = run(adapter_with(httpx.MockTransport(handler)).active_appointments("token"))

    assert [appointment.id for appointment in appointments] == ["appt-active"]
    assert all(request.method == "GET" for request in requests)


def test_ac4_the_adapter_exposes_no_write_booking_or_policy_operations() -> None:
    prohibited_names = (
        "book",
        "create_appointment",
        "reschedule",
        "cancel",
        "delete",
        "eligibility",
        "notice",
        "minimum_booking_notice",
        "default_slot",
    )
    public_methods = {
        name
        for name in dir(OpenEmrScheduleAdapter)
        if not name.startswith("_") and callable(getattr(OpenEmrScheduleAdapter, name))
    }
    assert public_methods == {"active_appointments", "availability", "office_hours", "closures"}
    assert not public_methods.intersection(prohibited_names)
