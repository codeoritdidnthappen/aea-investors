"""Synthetic tests for anonymous appointment-token issuance, resolution, and
cross-patient isolation (TICK-036), mirroring `test_slot_discovery.py`'s coverage of
`AnonymousSlotStore` (TICK-019) applied to `AnonymousAppointmentStore`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai_server.openemr.adapter import Appointment
from ai_server.privacy.gate import AnonymousAppointment, SchedulingContext
from ai_server.scheduling.appointments import (
    AnonymousAppointmentStore,
    AnonymousAppointmentToken,
    AppointmentDiscoveryService,
    AppointmentTokenError,
)

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 8, 18, 14, 30, tzinfo=TZ)


def appointment(hours_from_now: float = 2, identifier: str = "appt-1") -> Appointment:
    start = NOW + timedelta(hours=hours_from_now)
    return Appointment(
        id=identifier, status="booked", start=start, end=start + timedelta(minutes=30)
    )


class FakeAppointmentSource:
    def __init__(self, appointments: list[Appointment]) -> None:
        self._appointments = appointments
        self.requested_tokens: list[str] = []

    async def active_appointments(self, access_token: str) -> list[Appointment]:
        self.requested_tokens.append(access_token)
        return self._appointments


def run(coroutine):
    return asyncio.run(coroutine)


# --- AC1: reading appointments issues one token per non-cancelled appointment ------


def test_ac1_discovery_issues_one_token_per_appointment_bound_to_the_caller_patient() -> None:
    store = AnonymousAppointmentStore()
    source = FakeAppointmentSource([appointment(2, "appt-1"), appointment(5, "appt-2")])
    discovery = AppointmentDiscoveryService(source, store)

    tokens = run(discovery.current_appointments("delegated-token", "patient-a", NOW))

    assert len(tokens) == 2
    assert source.requested_tokens == ["delegated-token"]
    # Only timing fields and the token itself ever leave the store -- no OpenEMR id.
    fields = {f for f in AnonymousAppointmentToken.__dataclass_fields__}
    assert fields == {"appointment_token", "starts_at", "ends_at"}


def test_ac1_current_appointments_populate_only_the_approved_scheduling_context_shape() -> None:
    store = AnonymousAppointmentStore()
    source = FakeAppointmentSource([appointment(2), appointment(5, "appt-2")])
    discovery = AppointmentDiscoveryService(source, store)
    tokens = run(discovery.current_appointments("token", "patient-a", NOW))

    context = SchedulingContext(
        current_datetime=NOW,
        timezone="America/Chicago",
        current_appointments=[
            AnonymousAppointment(
                appointment_token=t.appointment_token, starts_at=t.starts_at, ends_at=t.ends_at
            )
            for t in tokens
        ],
    )

    dumped = context.model_dump(mode="json")
    assert set(dumped["current_appointments"][0]) == {"appointment_token", "starts_at", "ends_at"}
    assert all(a["appointment_token"].startswith("appt_") for a in dumped["current_appointments"])


def test_ac2_issued_tokens_are_unique_and_match_the_approved_appointment_token_shape() -> None:
    store = AnonymousAppointmentStore()
    tokens = [
        store.issue(appointment(index, f"appt-{index}"), "patient-a", NOW).appointment_token
        for index in range(50)
    ]

    assert len(set(tokens)) == 50
    for token in tokens:
        AnonymousAppointment(
            appointment_token=token, starts_at=NOW, ends_at=NOW + timedelta(minutes=1)
        )


# --- AC2: single-use resolve, expiry, and patient-bound (cross-session) isolation ---


def test_ac2_a_token_resolves_to_the_real_openemr_appointment_id_for_the_issuing_patient() -> None:
    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(2, "real-openemr-id"), "patient-a", NOW)

    resolved = store.resolve(issued.appointment_token, "patient-a", NOW)

    assert resolved == "real-openemr-id"


def test_ac2_a_token_is_single_purpose_and_cannot_be_reused() -> None:
    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(2, "appt-1"), "patient-a", NOW)

    store.resolve(issued.appointment_token, "patient-a", NOW)

    with pytest.raises(AppointmentTokenError, match="unknown or already used"):
        store.resolve(issued.appointment_token, "patient-a", NOW)


def test_ac2_a_token_expires_after_its_ttl() -> None:
    store = AnonymousAppointmentStore(ttl=timedelta(minutes=15))
    issued = store.issue(appointment(2, "appt-1"), "patient-a", NOW)

    with pytest.raises(AppointmentTokenError, match="expired"):
        store.resolve(issued.appointment_token, "patient-a", NOW + timedelta(minutes=15))


def test_ac2_cross_patient_negative_a_different_patients_id_cannot_resolve_the_token() -> None:
    """The live discipline of `evidence/TICK-028/BINDING_MATRIX.md` proved OpenEMR
    itself denies a cross-patient write; this proves the token store denies it too,
    entirely independently of OpenEMR, and never leaks whether the token exists."""
    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(2, "patient-a-appt"), "patient-a", NOW)

    with pytest.raises(AppointmentTokenError, match="unknown or already used"):
        store.resolve(issued.appointment_token, "patient-b", NOW)


def test_ac2_a_failed_cross_patient_resolve_still_consumes_the_token() -> None:
    """A wrong-patient attempt burns the token exactly like any other resolve attempt
    (matching `AnonymousSlotStore`'s discipline): the legitimate patient cannot be
    raced by a guessing attacker into a still-valid token afterward."""
    store = AnonymousAppointmentStore()
    issued = store.issue(appointment(2, "patient-a-appt"), "patient-a", NOW)

    with pytest.raises(AppointmentTokenError):
        store.resolve(issued.appointment_token, "patient-b", NOW)

    with pytest.raises(AppointmentTokenError, match="unknown or already used"):
        store.resolve(issued.appointment_token, "patient-a", NOW)


def test_ac2_discard_expired_removes_only_unresolved_expired_tokens() -> None:
    store = AnonymousAppointmentStore(ttl=timedelta(minutes=1))
    expiring = store.issue(appointment(1, "appt-1"), "patient-a", NOW)
    later = NOW + timedelta(minutes=2)
    fresh = store.issue(appointment(3, "appt-2"), "patient-a", later)

    store.discard_expired(later)

    with pytest.raises(AppointmentTokenError, match="unknown or already used"):
        store.resolve(expiring.appointment_token, "patient-a", later)
    assert store.resolve(fresh.appointment_token, "patient-a", later) == "appt-2"


def test_an_unknown_token_cannot_resolve_to_a_genuine_appointment() -> None:
    store = AnonymousAppointmentStore()

    with pytest.raises(AppointmentTokenError, match="unknown or already used"):
        store.resolve("appt_never-issued", "patient-a", NOW)


def test_an_expired_token_cannot_be_used_to_perform_a_cancellation() -> None:
    store = AnonymousAppointmentStore(ttl=timedelta(minutes=5))
    issued = store.issue(appointment(1, "appt-1"), "patient-a", NOW)

    def attempt_cancel(token: str, patient_id: str, at: datetime) -> str:
        """Stand in for the authoritative tool: it must reject before touching OpenEMR."""
        return store.resolve(token, patient_id, at)

    with pytest.raises(AppointmentTokenError):
        attempt_cancel(issued.appointment_token, "patient-a", NOW + timedelta(minutes=5))


def test_now_must_be_timezone_aware_for_discovery_and_the_store() -> None:
    store = AnonymousAppointmentStore()
    source = FakeAppointmentSource([appointment(1)])
    discovery = AppointmentDiscoveryService(source, store)
    naive = datetime(2026, 8, 18, 14, 30)

    with pytest.raises(ValueError, match="timezone-aware"):
        store.issue(appointment(1), "patient-a", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.resolve("appt_x", "patient-a", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        run(discovery.current_appointments("token", "patient-a", naive))


def test_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        AnonymousAppointmentStore(ttl=timedelta(0))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
