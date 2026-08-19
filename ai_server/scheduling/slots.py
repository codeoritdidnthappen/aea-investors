"""Turn genuinely open OpenEMR windows into short-lived, single-purpose tokens.

Provider availability, regular office hours, and holiday closures have no endpoint on
the pinned OpenEMR v8.3.0 release (evidence/TICK-001/ENDPOINT_MATRIX.md); the deferred
decision there names this ticket range explicitly and forbids a local database
workaround or an invented default (ARCHITECTURE.md ADR-3). This module therefore never
manufactures a candidate window itself: callers must supply one through
`SlotCandidateSource`, and no OpenEMR-backed implementation of that boundary exists
until a future release maps a real endpoint. What this module does own is turning
whatever candidates a mapped source eventually returns into tokens the external model
can select without ever seeing an OpenEMR identifier (FR-11, FR-20, NFR-5), and
rejecting a candidate that overlaps an already-booked appointment even if the source
claimed it was open.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ai_server.openemr.adapter import Appointment

_TOKEN_PREFIX = "slot_"
_TOKEN_ENTROPY_BYTES = 18
DEFAULT_SLOT_TOKEN_TTL = timedelta(minutes=15)


class SlotTokenError(Exception):
    """Raised when a slot token is unknown, already used, or expired."""


@dataclass(frozen=True)
class CandidateSlot:
    """One OpenEMR-sourced open-time window before anonymization."""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("candidate slot times must be timezone-aware")
        if self.ends_at <= self.starts_at:
            raise ValueError("candidate slot end must be after its start")


class SlotCandidateSource(Protocol):
    """The mapped-OpenEMR-endpoint boundary that can name open windows.

    No implementation is wired in production today (evidence/TICK-001/ENDPOINT_MATRIX.md);
    a caller must not satisfy this with locally invented office hours or defaults.
    """

    async def candidate_slots(self) -> list[CandidateSlot]:
        """Return every OpenEMR-sourced candidate window, unfiltered."""
        ...


class AppointmentSource(Protocol):
    """The mapped appointment-read boundary used to reject double-booked candidates."""

    async def active_appointments(self, access_token: str) -> list[Appointment]:
        """Return the launch-bound patient's non-cancelled appointments."""
        ...


@dataclass(frozen=True)
class AnonymousSlotToken:
    """A random, single-purpose token plus the only timing fields safe to expose."""

    slot_token: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class _StoredSlot:
    candidate: CandidateSlot
    expires_at: datetime


class AnonymousSlotStore:
    """Server-only mapping from a random token to a genuine OpenEMR window.

    A token is resolvable only inside the AI server, is removed the moment it resolves
    successfully so it cannot be reused (single-purpose), and expires independently of
    use so a stale candidate can never reach an appointment action (FR-20).
    """

    def __init__(self, ttl: timedelta = DEFAULT_SLOT_TOKEN_TTL) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._ttl = ttl
        self._slots: dict[str, _StoredSlot] = {}

    def issue(self, candidate: CandidateSlot, now: datetime) -> AnonymousSlotToken:
        """Mint one unique token for *candidate*, expiring `ttl` after *now*."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        token = _new_token()
        while token in self._slots:  # astronomically unlikely; keep uniqueness explicit
            token = _new_token()
        self._slots[token] = _StoredSlot(candidate, now + self._ttl)
        return AnonymousSlotToken(
            slot_token=token, starts_at=candidate.starts_at, ends_at=candidate.ends_at
        )

    def resolve(self, token: str, now: datetime) -> CandidateSlot:
        """Consume and return the genuine slot behind *token*.

        Raises `SlotTokenError` for an unknown, already-used, or expired token so an
        invalid token can never cause an appointment action (FR-20).
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        stored = self._slots.pop(token, None)
        if stored is None:
            raise SlotTokenError("slot token is unknown or already used")
        if now >= stored.expires_at:
            raise SlotTokenError("slot token has expired")
        return stored.candidate

    def discard_expired(self, now: datetime) -> None:
        """Drop expired, unresolved tokens so the mapping does not grow unbounded."""
        expired = [token for token, stored in self._slots.items() if now >= stored.expires_at]
        for token in expired:
            del self._slots[token]


def _new_token() -> str:
    return f"{_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"


class SlotDiscoveryService:
    """Filter OpenEMR-sourced candidates to genuinely open, future slots and tokenize them."""

    def __init__(
        self,
        source: SlotCandidateSource,
        appointments: AppointmentSource,
        store: AnonymousSlotStore,
    ) -> None:
        self._source = source
        self._appointments = appointments
        self._store = store

    async def open_slots(self, access_token: str, now: datetime) -> list[AnonymousSlotToken]:
        """Return anonymous tokens for future candidates that are not already booked (FR-11)."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        candidates = await self._source.candidate_slots()
        booked = await self._appointments.active_appointments(access_token)
        return [
            self._store.issue(candidate, now)
            for candidate in candidates
            if candidate.starts_at > now and not _overlaps_any(candidate, booked)
        ]


def _overlaps_any(candidate: CandidateSlot, appointments: list[Appointment]) -> bool:
    return any(
        candidate.starts_at < appointment.end and appointment.start < candidate.ends_at
        for appointment in appointments
    )
