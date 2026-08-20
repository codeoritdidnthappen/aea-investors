"""HTTP client for the module-added OpenEMR Portal API appointment-cancel resource.

`openemr_modules/aeai-portal-chat` registers `PUT /portal/patient/appointment/:auuid`
through OpenEMR's own module-extension events (`OpenEMR\\Events\\RestApiExtend\\*`),
the same mechanism TICK-017 used for the assessment-draft resource
(`ai_server/onboarding/draft_client.py`). This is this project's only client of that
route, and mirrors that adapter's shape exactly, including its most important
property: this adapter never stores or resolves a bearer token or a patient id itself.
OpenEMR derives and enforces the patient binding server-side from the token on every
call (`AppointmentCancelService::forPatient`, which requires both the caller's own
bound `puuid` and the requested `auuid` to match a single `AppointmentService::search`
row); the caller here only ever supplies the token and the appointment id it already
holds for the current session, never an OpenEMR-trusted identity.

Cancellation is always a status update (`evidence/TICK-001/ENDPOINT_MATRIX.md`, "Cancel
by status, retain history"; ARCHITECTURE.md ADR-4) -- this client has no delete method
and cannot express one.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import OpenEmrRequestError

_APPOINTMENT_PATH = "/portal/patient/appointment"


class AppointmentNotFoundError(Exception):
    """Raised for a 404: an unknown appointment id, or one belonging to another patient."""


class AppointmentAlreadyCancelledError(Exception):
    """Raised for a 409: the appointment was already cancelled."""


@dataclass(frozen=True)
class CancelledAppointment:
    """The OpenEMR-confirmed result of a cancellation call."""

    id: str
    status: str


class AppointmentCancelAdapter:
    """Cancels one of the caller's own appointments through the mapped Portal API route.

    Every method takes the caller's already-delegated bearer token; like
    `AssessmentDraftAdapter`, this adapter never stores, caches, or otherwise retains
    it, and never resolves "the logged-in patient" itself.
    """

    def __init__(self, settings: OpenEmrPortalSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def cancel(self, access_token: str, appointment_id: str) -> CancelledAppointment:
        """Cancel by OpenEMR status update; OpenEMR retains the record (never DELETE).

        Raises `AppointmentNotFoundError` for an unknown id or one that does not
        belong to the caller's own bound patient (proven with a live cross-patient
        negative test, `evidence/TICK-031`), `AppointmentAlreadyCancelledError` if it
        was already cancelled, and `OpenEmrRequestError` for anything else -- a caller
        never receives a `CancelledAppointment` unless OpenEMR actually confirmed one.
        """
        try:
            response = await self._client.put(
                f"{self._settings.portal_base_url}{_APPOINTMENT_PATH}/{appointment_id}",
                json={},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError("cancelling the appointment in OpenEMR failed") from exc
        if response.status_code == 404:
            raise AppointmentNotFoundError("no appointment with that id for this patient")
        if response.status_code == 409:
            raise AppointmentAlreadyCancelledError(_error_message(response))
        if response.status_code != 200:
            raise OpenEmrRequestError(
                f"OpenEMR appointment cancellation failed with status {response.status_code}"
            )
        return _cancelled_from_response(response)


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    payload = _safe_json(response)
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return "the OpenEMR appointment cancellation request failed"


def _cancelled_from_response(response: httpx.Response) -> CancelledAppointment:
    payload = _safe_json(response)
    if not isinstance(payload, dict):
        raise OpenEmrRequestError("OpenEMR returned an invalid cancellation response")
    identifier, status = payload.get("id"), payload.get("status")
    if not isinstance(identifier, str) or not isinstance(status, str):
        raise OpenEmrRequestError("OpenEMR returned an invalid cancellation response")
    return CancelledAppointment(id=identifier, status=status)
