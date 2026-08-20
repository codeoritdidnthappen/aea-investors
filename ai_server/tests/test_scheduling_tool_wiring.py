"""Tests for wiring the real booking tool into `ChatService` (TICK-034 AC5).

`_build_scheduling_tool` mirrors `_build_onboarding_service`'s tolerance of absent
OpenEMR configuration: every environment missing the Standard API/FHIR base URLs or
the admin-configured booking fields gets today's fixed no-action tool and no slot
discovery, instead of a startup failure.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from ai_server.app.chat import BookingTool, NoActionTool
from ai_server.app.main import _build_chat_service, _build_scheduling_tool
from ai_server.llm.groq import PlanningOutput
from ai_server.scheduling.slots import SlotDiscoveryService

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)

_REQUIRED_ENV = {
    "OPENEMR_API_BASE_URL": "https://openemr.test/apis/default/api",
    "OPENEMR_FHIR_BASE_URL": "https://openemr.test/apis/default/fhir",
    "AI_BOOKING_CATEGORY_ID": "5",
    "AI_BOOKING_TITLE": "Office Visit",
    "AI_BOOKING_FACILITY_ID": "9",
    "AI_BOOKING_BILLING_LOCATION_ID": "10",
}


def run(coroutine):
    return asyncio.run(coroutine)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_REQUIRED_ENV, "AI_BOOKING_PROVIDER_ID"):
        monkeypatch.delenv(name, raising=False)


def test_missing_openemr_settings_falls_back_to_the_no_action_tool_and_no_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    client = httpx.AsyncClient()

    factory, discovery = _build_scheduling_tool(client)

    assert discovery is None
    tool = factory("token", "patient-uuid", NOW)
    assert isinstance(tool, NoActionTool)
    result = run(tool.execute(PlanningOutput(intent="book", slot_token="slot_" + "a" * 20)))
    assert result.public_summary == "No scheduling action is available yet in this demo."


def test_missing_only_the_booking_fields_still_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENEMR_API_BASE_URL", _REQUIRED_ENV["OPENEMR_API_BASE_URL"])
    monkeypatch.setenv("OPENEMR_FHIR_BASE_URL", _REQUIRED_ENV["OPENEMR_FHIR_BASE_URL"])
    client = httpx.AsyncClient()

    factory, discovery = _build_scheduling_tool(client)

    assert discovery is None
    assert isinstance(factory("token", "patient-uuid", NOW), NoActionTool)


def test_every_required_setting_present_builds_the_real_tool_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    client = httpx.AsyncClient()

    factory, discovery = _build_scheduling_tool(client)

    assert isinstance(discovery, SlotDiscoveryService)
    tool = factory("token", "patient-uuid", NOW)
    assert isinstance(tool, BookingTool)
    assert tool.appointment_request.category_id == "5"
    assert tool.appointment_request.title == "Office Visit"
    assert tool.appointment_request.facility_id == "9"
    assert tool.appointment_request.billing_location_id == "10"
    assert tool.access_token == "token"
    assert tool.patient_id == "patient-uuid"
    assert tool.now == NOW


def test_the_same_slot_store_backs_both_discovery_and_the_booking_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot token `open_slots` issues in one turn must resolve in a later turn's
    `book` call: both must share one `AnonymousSlotStore` instance (TICK-034)."""
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    client = httpx.AsyncClient()

    factory, discovery = _build_scheduling_tool(client)
    assert discovery is not None
    tool = factory("token", "patient-uuid", NOW)
    assert isinstance(tool, BookingTool)

    # `discovery` issues tokens via its own `AnonymousSlotStore`; `tool.booking`
    # resolves them via `BookingService`'s. They must be the same store.
    assert discovery._store is tool.booking._store  # type: ignore[attr-defined]


def test_build_chat_service_falls_back_to_unavailable_without_groq_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_ZDR_VERIFIED_ON", raising=False)
    client = httpx.AsyncClient()

    service = _build_chat_service(client, client, lambda: NOW)

    assert service.workflow is None


def test_build_chat_service_wires_the_real_tool_when_groq_and_openemr_settings_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GROQ_ZDR_VERIFIED_ON", "2026-08-20")
    client = httpx.AsyncClient()

    service = _build_chat_service(client, client, lambda: NOW)

    assert service.workflow is not None
    assert service.slot_discovery is not None
    tool = service.tool_factory("token", "patient-uuid", NOW)
    assert isinstance(tool, BookingTool)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
