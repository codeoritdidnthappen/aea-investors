"""Synthetic tests for anonymous slot discovery, tokenization, and resolution."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest

from ai_server.llm.tools import SLOT_TOKEN_PATTERN, BookAppointmentArguments
from ai_server.openemr.adapter import Appointment
from ai_server.scheduling.slots import (
    AnonymousSlotStore,
    AnonymousSlotToken,
    CandidateSlot,
    SlotDiscoveryService,
    SlotTokenError,
)

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 18, 14, 30, tzinfo=TZ)


def candidate(hours_from_now: float, duration_minutes: int = 30) -> CandidateSlot:
    start = NOW + timedelta(hours=hours_from_now)
    return CandidateSlot(starts_at=start, ends_at=start + timedelta(minutes=duration_minutes))


def booked(start: datetime, end: datetime, identifier: str = "appt-1") -> Appointment:
    return Appointment(id=identifier, status="booked", start=start, end=end, description=None)


class FakeCandidateSource:
    def __init__(self, candidates: list[CandidateSlot]) -> None:
        self._candidates = candidates

    async def candidate_slots(self) -> list[CandidateSlot]:
        return self._candidates


class FakeAppointmentSource:
    def __init__(self, appointments: list[Appointment]) -> None:
        self._appointments = appointments
        self.requested_tokens: list[str] = []

    async def active_appointments(self, access_token: str) -> list[Appointment]:
        self.requested_tokens.append(access_token)
        return self._appointments


def run(coroutine):
    return asyncio.run(coroutine)


def service(
    candidates: list[CandidateSlot],
    appointments: list[Appointment],
    store: AnonymousSlotStore | None = None,
) -> tuple[SlotDiscoveryService, AnonymousSlotStore, FakeAppointmentSource]:
    appointment_source = FakeAppointmentSource(appointments)
    active_store = store or AnonymousSlotStore()
    return (
        SlotDiscoveryService(FakeCandidateSource(candidates), appointment_source, active_store),
        active_store,
        appointment_source,
    )


def test_candidate_slot_rejects_naive_and_inverted_windows() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CandidateSlot(starts_at=datetime(2026, 8, 25, 13, 0), ends_at=NOW)
    with pytest.raises(ValueError, match="end must be after its start"):
        CandidateSlot(starts_at=NOW, ends_at=NOW - timedelta(minutes=1))


def test_ac1_only_future_candidates_are_presented() -> None:
    past = candidate(-1)
    future = candidate(2)
    discovery, _, _ = service([past, future], [])

    tokens = run(discovery.open_slots("token", NOW))

    assert [t.starts_at for t in tokens] == [future.starts_at]


def test_ac1_a_candidate_overlapping_an_active_appointment_is_not_genuinely_open() -> None:
    open_candidate = candidate(2)
    double_booked = candidate(5)
    conflict = booked(double_booked.starts_at, double_booked.ends_at)
    discovery, _, _ = service([open_candidate, double_booked], [conflict])

    tokens = run(discovery.open_slots("token", NOW))

    assert [t.starts_at for t in tokens] == [open_candidate.starts_at]


def test_ac1_back_to_back_candidates_touching_an_appointment_boundary_are_open() -> None:
    appointment = booked(NOW + timedelta(hours=1), NOW + timedelta(hours=1, minutes=30))
    right_before = CandidateSlot(
        starts_at=NOW + timedelta(hours=1) - timedelta(minutes=30), ends_at=NOW + timedelta(hours=1)
    )
    right_after = CandidateSlot(
        starts_at=appointment.end, ends_at=appointment.end + timedelta(minutes=30)
    )
    discovery, _, _ = service([right_before, right_after], [appointment])

    tokens = run(discovery.open_slots("token", NOW))

    assert {t.starts_at for t in tokens} == {right_before.starts_at, right_after.starts_at}


def test_ac1_cancelled_appointments_are_the_caller_openemr_adapters_concern() -> None:
    """This service trusts whatever active_appointments already returned, cancelled or not.

    Cancellation filtering is the OpenEmrScheduleAdapter's contract (TICK-018); passing
    only non-cancelled appointments here is the integration point, so a slot conflicting
    with an appointment the adapter surfaced is still treated as booked.
    """
    conflict_candidate = candidate(2)
    conflicting = booked(conflict_candidate.starts_at, conflict_candidate.ends_at, "appt-active")
    discovery, _, appointment_source = service([conflict_candidate], [conflicting])

    tokens = run(discovery.open_slots("delegated-token", NOW))

    assert tokens == []
    assert appointment_source.requested_tokens == ["delegated-token"]


def test_ac2_issued_tokens_are_unique_and_match_the_approved_slot_token_shape() -> None:
    store = AnonymousSlotStore()
    tokens = [store.issue(candidate(index), NOW).slot_token for index in range(50)]

    assert len(set(tokens)) == 50
    for token in tokens:
        BookAppointmentArguments(slot_token=token)


def test_ac2_a_token_expires_after_its_ttl() -> None:
    store = AnonymousSlotStore(ttl=timedelta(minutes=15))
    issued = store.issue(candidate(1), NOW)

    with pytest.raises(SlotTokenError, match="expired"):
        store.resolve(issued.slot_token, NOW + timedelta(minutes=15))


def test_ac2_a_token_is_single_purpose_and_cannot_be_reused() -> None:
    store = AnonymousSlotStore()
    open_candidate = candidate(1)
    issued = store.issue(open_candidate, NOW)

    resolved = store.resolve(issued.slot_token, NOW)

    assert resolved == open_candidate
    with pytest.raises(SlotTokenError, match="unknown or already used"):
        store.resolve(issued.slot_token, NOW)


def test_ac2_discard_expired_removes_only_unresolved_expired_tokens() -> None:
    store = AnonymousSlotStore(ttl=timedelta(minutes=1))
    expiring = store.issue(candidate(1), NOW)
    later = NOW + timedelta(minutes=2)
    fresh = store.issue(candidate(3), later)

    store.discard_expired(later)

    with pytest.raises(SlotTokenError, match="unknown or already used"):
        store.resolve(expiring.slot_token, later)
    assert store.resolve(fresh.slot_token, later) == candidate(3)


def test_ac3_a_slot_token_carries_only_timing_fields_no_openemr_identifier() -> None:
    fields = {f for f in AnonymousSlotToken.__dataclass_fields__}
    assert fields == {"slot_token", "starts_at", "ends_at"}


def test_ac3_every_issued_token_is_accepted_by_the_published_tool_argument() -> None:
    """The shape check moved with the token (TICK-064).

    It used to build a `SchedulingContext` -- the outbound Groq scheduling payload, which
    D13 deleted along with scheduling egress. A slot token's only published destination
    now is `book_appointment`'s argument, so that is what has to accept every token this
    service issues.
    """
    discovery, _, _ = service([candidate(1), candidate(2)], [])
    tokens = run(discovery.open_slots("token", NOW))

    assert tokens
    for issued in tokens:
        assert re.match(SLOT_TOKEN_PATTERN, issued.slot_token)
        arguments = BookAppointmentArguments(slot_token=issued.slot_token)
        assert arguments.model_dump() == {"slot_token": issued.slot_token}


def test_ac4_an_unknown_token_cannot_resolve_to_a_genuine_slot() -> None:
    store = AnonymousSlotStore()

    with pytest.raises(SlotTokenError, match="unknown or already used"):
        store.resolve("slot_never-issued", NOW)


def test_ac4_an_expired_token_cannot_be_used_to_perform_an_appointment_action() -> None:
    store = AnonymousSlotStore(ttl=timedelta(minutes=5))
    issued = store.issue(candidate(1), NOW)

    def attempt_booking(token: str, at: datetime) -> str:
        """Stand in for an authoritative tool: it must reject before touching OpenEMR."""
        store.resolve(token, at)
        return "booked"

    with pytest.raises(SlotTokenError):
        attempt_booking(issued.slot_token, NOW + timedelta(minutes=5))


def test_now_must_be_timezone_aware_for_discovery_and_the_store() -> None:
    store = AnonymousSlotStore()
    discovery, _, _ = service([candidate(1)], [])
    naive = datetime(2026, 8, 18, 14, 30)

    with pytest.raises(ValueError, match="timezone-aware"):
        store.issue(candidate(1), naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.resolve("slot_x", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        run(discovery.open_slots("token", naive))
