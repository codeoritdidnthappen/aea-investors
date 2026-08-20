"""Write only a patient's explicitly confirmed identity to their own OpenEMR record.

Only name, date of birth, and address may change the logged-in patient's chart, and
only once every field has been explicitly confirmed or corrected (FR-6, FR-17, FR-26,
NFR-25). This module writes through the Standard API endpoint recorded as supported in
`evidence/TICK-001/ENDPOINT_MATRIX.md` ("Write confirmed demographics") and exercised in
`evidence/TICK-001/PROBE_EVIDENCE.md` ("Update confirmed synthetic demographic").

That evidence also records what remains unproven: OpenEMR's write route has no source-
verified patient-identity enforcement of its own, and the local probe used a user-scoped
token, not a SMART launch-bound patient-context token. This module never closes that gap
by trusting OpenEMR to reject the wrong patient; instead it never asks OpenEMR to decide.
Every call takes the target patient id as an explicit argument from the caller, exactly
like `OpenEmrScheduleAdapter` takes a bearer token per call rather than storing one -
this module never stores, infers, or looks up which patient is "logged in". The caller
(the authenticated session/orchestration layer) is responsible for never supplying any
id other than the one already established for the current session.

`ConfirmedIdentity` can only be constructed by `confirm_identity`, which refuses unless
name, date of birth, and address were all explicitly confirmed or corrected. An
unconfirmed, partial, revoked, or failed OCR result therefore has no path to a write
(AC1, AC3): there is no constructor that accepts a missing field.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError


class IdentityNotConfirmedError(Exception):
    """Raised when name, date of birth, and address were not all explicitly confirmed."""


@dataclass(frozen=True)
class ConfirmedIdentity:
    """Only a fully confirmed identity; every field but `family_name` is non-empty.

    `given_name` and `family_name` are kept separate rather than one `name` string:
    OpenEMR's write endpoint takes them as separate fields, and no join-then-split
    scheme can recover an internally-spaced family name (e.g. "Van Der Berg") from a
    joined string without guessing. `family_name` alone may be empty for a confirmed
    mononym; `confirm_identity` still requires every other field.
    """

    given_name: str
    family_name: str
    date_of_birth: str
    address: str


def confirm_identity(
    given_name: str | None,
    family_name: str | None,
    date_of_birth: str | None,
    address: str | None,
) -> ConfirmedIdentity:
    """Build a write-eligible identity only once every required field is confirmed (AC1).

    Each argument is the value the patient explicitly confirmed or corrected, never
    a raw OCR value passed through automatically. `None` or blank covers every
    no-write case in one path: never extracted, extracted but not yet confirmed,
    revoked, or failed OCR (AC3) - all refuse identically instead of writing a
    partial record. `family_name` is the one field allowed to be confirmed as empty
    (a mononym), so it is not part of this check.
    """
    fields = {"given_name": given_name, "date_of_birth": date_of_birth, "address": address}
    missing = [field for field, value in fields.items() if not value]
    if missing:
        raise IdentityNotConfirmedError(f"identity fields not confirmed: {', '.join(missing)}")
    assert given_name and date_of_birth and address  # narrows for the type checker
    return ConfirmedIdentity(
        given_name=given_name,
        family_name=family_name or "",
        date_of_birth=date_of_birth,
        address=address,
    )


@dataclass(frozen=True)
class OpenEmrDemographicsSettings:
    """Validated configuration for the OpenEMR Standard API patient-write boundary."""

    api_base_url: str

    @classmethod
    def from_environment(cls) -> OpenEmrDemographicsSettings:
        """Parse the required Standard API base URL once during application startup."""
        base_url = os.environ.get("OPENEMR_API_BASE_URL")
        if not base_url:
            raise OpenEmrConfigurationError("OPENEMR_API_BASE_URL is required")
        return cls(api_base_url=base_url.rstrip("/"))


class OpenEmrDemographicsAdapter:
    """Writes a confirmed identity to exactly the patient id the caller supplies.

    Every method takes the caller's already-delegated bearer token and the caller's
    already-established patient id; this adapter never stores, caches, or otherwise
    retains either one, and never resolves "the logged-in patient" itself.
    """

    def __init__(self, settings: OpenEmrDemographicsSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def write_confirmed_demographics(
        self, access_token: str, patient_uuid: str, identity: ConfirmedIdentity
    ) -> None:
        """Write only confirmed name, date of birth, and address (FR-26, AC2).

        `identity` can only exist via `confirm_identity`, so there is no code path
        from an unconfirmed, partial, revoked, or failed OCR value to this call.
        """
        body = {
            "fname": identity.given_name,
            "lname": identity.family_name,
            "DOB": identity.date_of_birth,
            "street": identity.address,
        }
        try:
            response = await self._client.put(
                f"{self._settings.api_base_url}/patient/{patient_uuid}",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError("writing confirmed demographics to OpenEMR failed") from exc
        if response.status_code != 200:
            raise OpenEmrRequestError("writing confirmed demographics to OpenEMR failed")
