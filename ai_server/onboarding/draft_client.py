"""HTTP client for the module-added OpenEMR Portal API assessment-draft resource.

`openemr_modules/aeai-portal-chat` (proven live in
`evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md`) registers `POST/GET/PUT
/portal/patient/assessment[/:auuid]` through OpenEMR's own module-extension events.
This module is this project's only client of that route; it mirrors
`ai_server/openemr/demographics.py`'s adapter shape exactly, including its most
important property: this adapter never stores or resolves a bearer token or a
patient id itself. OpenEMR derives and enforces the patient binding server-side from
the token on every call (`HttpRestRequest::getPatientUUIDString()`); the caller here
only ever supplies the token it already holds for the current session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError

_DRAFT_PATH = "/portal/patient/assessment"


class AssessmentDraftNotFoundError(Exception):
    """Raised for a 404: an unknown draft, or one that belongs to another patient."""


class AssessmentDraftConflictError(Exception):
    """Raised for a 409: the draft is already completed, or changed concurrently."""


class AssessmentDraftValidationError(Exception):
    """Raised for a 400: OpenEMR's own field validation rejected the submitted body."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


@dataclass(frozen=True)
class AssessmentDraft:
    """One assessment draft exactly as OpenEMR returned it."""

    uuid: str
    status: str
    fields: dict[str, object]


@dataclass(frozen=True)
class OpenEmrPortalSettings:
    """Validated configuration for the OpenEMR Portal API boundary."""

    portal_base_url: str

    @classmethod
    def from_environment(cls) -> OpenEmrPortalSettings:
        """Parse the required Portal API base URL once during application startup."""
        base_url = os.environ.get("OPENEMR_PORTAL_BASE_URL")
        if not base_url:
            raise OpenEmrConfigurationError("OPENEMR_PORTAL_BASE_URL is required")
        return cls(portal_base_url=base_url.rstrip("/"))


class AssessmentDraftAdapter:
    """Checkpoints assessment-draft fields 6-9 through the mapped Portal API route.

    Every method takes the caller's already-delegated bearer token; like
    `OpenEmrDemographicsAdapter`, this adapter never stores, caches, or otherwise
    retains it.
    """

    def __init__(self, settings: OpenEmrPortalSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def create(self, access_token: str, fields: dict[str, object]) -> AssessmentDraft:
        """Start a new draft, optionally pre-populated with already-checkpointed fields."""
        response = await self._send("POST", _DRAFT_PATH, access_token, fields)
        if response.status_code != 201:
            raise self._error_for(response)
        return _draft_from_response(response)

    async def read(self, access_token: str, uuid: str) -> AssessmentDraft:
        """Reload one draft by id, scoped server-side to the caller's own patient."""
        response = await self._send("GET", f"{_DRAFT_PATH}/{uuid}", access_token, None)
        if response.status_code != 200:
            raise self._error_for(response)
        return _draft_from_response(response)

    async def update(
        self,
        access_token: str,
        uuid: str,
        fields: dict[str, object],
        *,
        complete: bool = False,
    ) -> AssessmentDraft:
        """Checkpoint `fields` into the draft, or finalize it when `complete=True`.

        OpenEMR merges `fields` into the existing payload server-side, so only the
        newly submitted field(s) need to be sent -- never the full accumulated set.
        """
        body: dict[str, object] = dict(fields)
        if complete:
            body["status"] = "completed"
        response = await self._send("PUT", f"{_DRAFT_PATH}/{uuid}", access_token, body)
        if response.status_code != 200:
            raise self._error_for(response)
        return _draft_from_response(response)

    async def _send(
        self, method: str, path: str, access_token: str, body: dict[str, object] | None
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                f"{self._settings.portal_base_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError(
                f"{method} to the OpenEMR assessment draft endpoint failed"
            ) from exc

    def _error_for(self, response: httpx.Response) -> Exception:
        if response.status_code == 404:
            return AssessmentDraftNotFoundError("no assessment draft with that id for this patient")
        if response.status_code == 409:
            return AssessmentDraftConflictError(_error_message(response))
        if response.status_code == 400:
            message, details = _error_message_and_details(response)
            return AssessmentDraftValidationError(message, details)
        return OpenEmrRequestError(
            f"OpenEMR assessment draft request failed with status {response.status_code}"
        )


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    payload = _safe_json(response)
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return "the OpenEMR assessment draft request failed"


def _error_message_and_details(response: httpx.Response) -> tuple[str, list[str]]:
    payload = _safe_json(response)
    message = _error_message(response)
    details = payload.get("details") if isinstance(payload, dict) else None
    if not isinstance(details, list) or any(not isinstance(item, str) for item in details):
        details = []
    return message, details


def _draft_from_response(response: httpx.Response) -> AssessmentDraft:
    payload = _safe_json(response)
    if not isinstance(payload, dict):
        raise OpenEmrRequestError("OpenEMR returned an invalid assessment draft response")
    uuid, status, fields = payload.get("uuid"), payload.get("status"), payload.get("fields")
    if not isinstance(uuid, str) or not isinstance(status, str) or not isinstance(fields, dict):
        raise OpenEmrRequestError("OpenEMR returned an invalid assessment draft response")
    return AssessmentDraft(uuid=uuid, status=status, fields=fields)
