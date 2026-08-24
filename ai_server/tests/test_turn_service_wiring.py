"""Tests for wiring the real backing services into `ModelTurnService` (TICK-063).

Renamed from `test_scheduling_tool_wiring.py` by TICK-064. That file also covered
`_build_scheduling_tool` and `_build_chat_service`, which built the Groq planner's
`BookingTool`/`NoActionTool` pair; D13 moved planning to the local model and both
builders were deleted with it. The TICK-063 half is unchanged and kept here: every
service is optional and independently degradable, so an environment missing the
Standard/FHIR/Portal API base URLs or the admin-configured booking fields loses the
tools that need them and keeps the rest of the turn, instead of failing startup.

`ai_server.app.chat.BookingToolSettings` is exercised through that wiring rather than
directly, which is the only way it is reached in production.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ai_server.app.auth import SessionStore
from ai_server.app.chat import BookingToolSettings, NoMappedCandidateSource
from ai_server.app.main import _build_model_turn_service
from ai_server.openemr.adapter import OpenEmrConfigurationError
from ai_server.scheduling.appointments import AppointmentDiscoveryService
from ai_server.scheduling.slots import SlotDiscoveryService

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)

_REQUIRED_ENV = {
    "OPENEMR_API_BASE_URL": "https://openemr.test/apis/default/api",
    "OPENEMR_FHIR_BASE_URL": "https://openemr.test/apis/default/fhir",
    "OPENEMR_PORTAL_BASE_URL": "https://openemr.test/apis/default",
    "AI_BOOKING_CATEGORY_ID": "5",
    "AI_BOOKING_TITLE": "Office Visit",
    "AI_BOOKING_FACILITY_ID": "9",
    "AI_BOOKING_BILLING_LOCATION_ID": "10",
}


def run(coroutine):
    return asyncio.run(coroutine)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_REQUIRED_ENV, "AI_BOOKING_PROVIDER_ID", "GROQ_API_KEY", "GROQ_ZDR_VERIFIED_ON"):
        monkeypatch.delenv(name, raising=False)


def _store() -> SessionStore:
    """A `SessionStore` that is never opened: the builder only stores the reference."""
    return SessionStore(Path("/tmp/tick063-wiring-unused.sqlite3"), b"k" * 32)


def _build(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    return _build_model_turn_service(
        httpx.AsyncClient(), httpx.AsyncClient(), _store(), lambda: NOW
    )


# --- TICK-063: the model-first turn service degrades one capability at a time -------


def test_the_turn_service_degrades_when_the_local_model_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D12: with no deterministic fallback, an absent `LLM_MODEL` degrades the whole
    chat to the honest unavailable message rather than failing startup."""
    _clear_env(monkeypatch)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    service = _build(monkeypatch)

    assert service.client is None
    assert service.services.slot_discovery is None


def test_missing_openemr_settings_keep_the_model_and_drop_the_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A demo missing the OpenEMR base URLs loses the tools that need them and keeps
    the rest of the turn."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b-instruct-q4_K_M")

    service = _build(monkeypatch)

    assert service.client is not None
    assert service.services.booking is None
    assert service.services.demographics is None
    assert service.services.ocr is not None  # OCR is local and needs no OpenEMR settings


def test_a_configured_environment_wires_every_tool_to_a_real_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b-instruct-q4_K_M")

    service = _build(monkeypatch)

    services = service.services
    assert isinstance(services.slot_discovery, SlotDiscoveryService)
    assert isinstance(services.appointment_discovery, AppointmentDiscoveryService)
    assert services.booking is not None
    assert services.cancellation is not None
    assert services.appointment_request is not None
    assert services.demographics is not None
    assert services.onboarding is not None
    assert service.cursors is not None


def test_the_configured_booking_fields_reach_the_appointment_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BookingToolSettings` is the boundary that keeps this demo's office configuration
    out of `booking.py` (TICK-034). Its values must arrive on the request the booking
    tool actually sends, not merely be parsed."""
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AI_BOOKING_PROVIDER_ID", "22")
    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b-instruct-q4_K_M")

    request = _build(monkeypatch).services.appointment_request

    assert request is not None
    assert request.category_id == "5"
    assert request.title == "Office Visit"
    assert request.facility_id == "9"
    assert request.billing_location_id == "10"
    assert request.provider_id == "22"


def test_an_absent_provider_id_is_none_rather_than_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AI_BOOKING_PROVIDER_ID` is the one optional field; an empty string would be a
    provider id OpenEMR does not have."""
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AI_BOOKING_PROVIDER_ID", "")

    assert BookingToolSettings.from_environment().provider_id is None


@pytest.mark.parametrize(
    "missing", sorted(name for name in _REQUIRED_ENV if name.startswith("AI_BOOKING_"))
)
def test_every_required_booking_field_is_named_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """A configuration error that does not say which value is absent costs an operator
    a guess per field."""
    _clear_env(monkeypatch)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(OpenEmrConfigurationError) as raised:
        BookingToolSettings.from_environment()

    assert missing in str(raised.value)


def test_the_candidate_source_reports_no_slots_rather_than_inventing_them() -> None:
    """ADR-3: no OpenEMR endpoint exists for provider availability on the pinned
    release, and an invented default is forbidden. Empty is the honest answer."""
    assert run(NoMappedCandidateSource().candidate_slots()) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
