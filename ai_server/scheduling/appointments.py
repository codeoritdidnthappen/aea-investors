"""Turn the patient's own current OpenEMR appointments into short-lived, single-purpose
tokens the external model can reference, mirroring `ai_server.scheduling.slots
.AnonymousSlotStore`'s discipline (TICK-019) applied to a different source list
(TICK-036): the patient's existing appointments (TICK-018's `active_appointments`,
cancelled ones already excluded, FR-15) instead of open candidate windows.

Unlike a slot, an appointment token identifies one specific patient's own record, so a
token minted while resolving one session's appointments must never resolve for a
different patient -- `issue()`/`resolve()` therefore also bind to the caller's own
patient id and reject a `resolve()` attempt made under any other patient id the same
way an unknown token is rejected, so a stolen or guessed token cannot be used
cross-patient even though OpenEMR's own `AppointmentCancelService::forPatient` would
independently refuse the write too (`ai_server/scheduling/cancel.py`). Like
`AnonymousSlotStore.resolve`, any resolve attempt -- successful or not -- consumes the
token, so a wrong-patient guess cannot be retried.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from ai_server.openemr.adapter import Appointment
from ai_server.scheduling.slots import AppointmentSource

_TOKEN_PREFIX = "appt_"
_TOKEN_ENTROPY_BYTES = 18
DEFAULT_APPOINTMENT_TOKEN_TTL = timedelta(minutes=15)


class AppointmentTokenError(Exception):
    """Raised when an appointment token is unknown, already used, expired, or was
    issued to a different patient than the one attempting to resolve it."""


@dataclass(frozen=True)
class AnonymousAppointmentToken:
    """A random, single-purpose token plus the only timing fields safe to expose."""

    appointment_token: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class _StoredAppointment:
    appointment_id: str
    patient_id: str
    expires_at: datetime


class AnonymousAppointmentStore:
    """Server-only mapping from a random token to a genuine OpenEMR appointment id.

    A token is resolvable only inside the AI server and only by the patient id it was
    issued for, is removed on the first resolve attempt regardless of outcome so it
    cannot be reused or brute-forced (single-purpose), and expires independently of use
    so a stale reference can never reach an appointment action (FR-20).
    """

    def __init__(self, ttl: timedelta = DEFAULT_APPOINTMENT_TOKEN_TTL) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._ttl = ttl
        self._appointments: dict[str, _StoredAppointment] = {}

    def issue(
        self, appointment: Appointment, patient_id: str, now: datetime
    ) -> AnonymousAppointmentToken:
        """Mint one unique token for *appointment*, bound to *patient_id*."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        token = _new_token()
        while token in self._appointments:  # astronomically unlikely; keep uniqueness explicit
            token = _new_token()
        self._appointments[token] = _StoredAppointment(
            appointment_id=appointment.id, patient_id=patient_id, expires_at=now + self._ttl
        )
        return AnonymousAppointmentToken(
            appointment_token=token, starts_at=appointment.start, ends_at=appointment.end
        )

    def resolve(self, token: str, patient_id: str, now: datetime) -> str:
        """Consume and return the genuine appointment id behind *token*.

        Raises `AppointmentTokenError` for an unknown, already-used, or expired token,
        or one resolved under a different patient id than the one it was issued for
        (FR-16, FR-20) -- an invalid or cross-patient token can never cause an
        appointment action.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        stored = self._appointments.pop(token, None)
        if stored is None:
            raise AppointmentTokenError("appointment token is unknown or already used")
        if now >= stored.expires_at:
            raise AppointmentTokenError("appointment token has expired")
        if stored.patient_id != patient_id:
            # Reported identically to "unknown" so a cross-patient attempt learns
            # nothing about whether the token exists.
            raise AppointmentTokenError("appointment token is unknown or already used")
        return stored.appointment_id

    def discard_expired(self, now: datetime) -> None:
        """Drop expired, unresolved tokens so the mapping does not grow unbounded."""
        expired = [
            token for token, stored in self._appointments.items() if now >= stored.expires_at
        ]
        for token in expired:
            del self._appointments[token]


def _new_token() -> str:
    return f"{_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"


class AppointmentDiscoveryService:
    """Read the patient's own current appointments and tokenize each one (FR-9)."""

    def __init__(self, appointments: AppointmentSource, store: AnonymousAppointmentStore) -> None:
        self._appointments = appointments
        self._store = store

    async def current_appointments(
        self, access_token: str, patient_id: str, now: datetime
    ) -> list[AnonymousAppointmentToken]:
        """Return anonymous tokens for the caller's own non-cancelled appointments."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        appointments = await self._appointments.active_appointments(access_token)
        return [self._store.issue(appointment, patient_id, now) for appointment in appointments]
