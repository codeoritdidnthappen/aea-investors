"""Read-only, user-scoped scheduling adapter for the pinned OpenEMR v8.3.0 release.

Only the operations recorded as supported in `evidence/TICK-001/ENDPOINT_MATRIX.md`
are implemented. Provider availability, regular office hours, and holiday closures
have no endpoint on the pinned release and fail explicitly instead of falling back to
a database query or an invented default (ARCHITECTURE.md ADR-3). This module adds no
booking, rescheduling, cancellation, eligibility, or notice behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import httpx

_EXCLUDED_STATUSES = frozenset({"cancelled"})

_GAP_MESSAGE = (
    "{operation} has no mapped OpenEMR v8.3.0 endpoint "
    "(evidence/TICK-001/ENDPOINT_MATRIX.md); no local fallback is permitted"
)


class OpenEmrConfigurationError(Exception):
    """Raised when a required OpenEMR adapter deployment setting is absent."""


class OpenEmrOperationNotMappedError(Exception):
    """Raised for a scheduling operation with no endpoint on the pinned release."""


class OpenEmrRequestError(Exception):
    """Raised when a call to a mapped OpenEMR endpoint fails or returns unusable data."""


@dataclass(frozen=True)
class Appointment:
    """A single active OpenEMR appointment, trimmed to chat-safe fields."""

    id: str
    status: str
    start: datetime
    end: datetime
    description: str | None = None


@dataclass(frozen=True)
class OpenEmrScheduleSettings:
    """Validated configuration for the OpenEMR FHIR scheduling boundary."""

    fhir_base_url: str

    @classmethod
    def from_environment(cls) -> OpenEmrScheduleSettings:
        """Parse the required FHIR base URL once during application startup."""
        base_url = os.environ.get("OPENEMR_FHIR_BASE_URL")
        if not base_url:
            raise OpenEmrConfigurationError("OPENEMR_FHIR_BASE_URL is required")
        return cls(fhir_base_url=base_url.rstrip("/"))


class OpenEmrScheduleAdapter:
    """Translates scheduling reads into the mapped OpenEMR FHIR endpoint only.

    Every method takes the caller's already-delegated bearer token; this adapter
    never stores, caches, or otherwise retains a token itself.
    """

    def __init__(self, settings: OpenEmrScheduleSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def active_appointments(self, access_token: str) -> list[Appointment]:
        """Return the launch-bound patient's non-cancelled appointments.

        Cancelled appointments are filtered out of this result but are never
        deleted or otherwise mutated; OpenEMR retains the record (FR-15).
        """
        try:
            response = await self._client.get(
                f"{self._settings.fhir_base_url}/Appointment",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError("reading appointments from OpenEMR failed") from exc
        if response.status_code != 200:
            raise OpenEmrRequestError("reading appointments from OpenEMR failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenEmrRequestError("OpenEMR returned an invalid appointment response") from exc
        try:
            appointments = _appointments_from_bundle(payload)
        except ValueError as exc:
            raise OpenEmrRequestError(str(exc)) from exc
        return [
            appointment
            for appointment in appointments
            if appointment.status not in _EXCLUDED_STATUSES
        ]

    async def availability(self) -> list[object]:
        """Fail explicitly: no provider-availability endpoint exists on v8.3.0 (FR-10)."""
        raise OpenEmrOperationNotMappedError(_GAP_MESSAGE.format(operation="provider availability"))

    async def office_hours(self) -> list[object]:
        """Fail explicitly: no office-hours endpoint exists on v8.3.0 (FR-10)."""
        raise OpenEmrOperationNotMappedError(_GAP_MESSAGE.format(operation="regular office hours"))

    async def closures(self) -> list[object]:
        """Fail explicitly: no closure/holiday endpoint exists on v8.3.0 (FR-10)."""
        raise OpenEmrOperationNotMappedError(
            _GAP_MESSAGE.format(operation="holiday or exceptional closures")
        )


def _appointments_from_bundle(payload: object) -> list[Appointment]:
    if not isinstance(payload, dict) or payload.get("resourceType") != "Bundle":
        raise ValueError("OpenEMR returned an invalid appointment bundle")
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        raise ValueError("OpenEMR returned an invalid appointment bundle")
    return [_appointment_from_entry(entry) for entry in entries]


def _appointment_from_entry(entry: object) -> Appointment:
    if not isinstance(entry, dict):
        raise ValueError("OpenEMR returned an invalid appointment bundle entry")
    resource = entry.get("resource")
    if not isinstance(resource, dict) or resource.get("resourceType") != "Appointment":
        raise ValueError("OpenEMR returned an invalid appointment resource")
    identifier, status, start, end = (
        resource.get("id"),
        resource.get("status"),
        resource.get("start"),
        resource.get("end"),
    )
    if not all(isinstance(value, str) and value for value in (identifier, status, start, end)):
        raise ValueError("OpenEMR appointment resource is missing a required field")
    description = resource.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("OpenEMR appointment resource has an invalid description")
    return Appointment(
        id=identifier,
        status=status,
        start=_parse_aware_datetime(start),
        end=_parse_aware_datetime(end),
        description=description,
    )


def _parse_aware_datetime(value: str) -> datetime:
    """Parse a FHIR `dateTime`, preserving its explicit UTC offset for DST correctness."""
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("OpenEMR appointment time is not valid ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("OpenEMR appointment time has no explicit timezone")
    return parsed
