"""Write only a patient's explicitly confirmed identity to their own OpenEMR record.

Only name, date of birth, and address may change the logged-in patient's chart, and
only once every field has been explicitly confirmed or corrected (FR-6, FR-17, FR-26,
NFR-25). This module writes through the module-added Portal API route
`openemr_modules/aeai-portal-chat` registers (`PUT /portal/patient/demographics`,
TICK-042), not the Standard API's `PUT /api/patient/:puuid` this module targeted before:
that Standard API route is gated by a staff ACL check
(`RestConfig::request_authorization_check($request, "patients", "demo")` ->
`AclMain::aclCheckCore()` against a logged-in staff `authUser`), never an OAuth scope --
structurally unreachable for a genuine patient-context bearer token, the identical gap
TICK-040 already root-caused and fixed for booking. See
`tickets/TICK-042-fix-demographics-write-unreachable.md`.

The module route resolves the target patient server-side from the bearer token
(`HttpRestRequest::getPatientUUIDString()`), never from caller-supplied input -- this
adapter no longer sends or needs a patient id at all, matching
`OpenEmrBookingAdapter`'s/`AppointmentCancelAdapter`'s own contract exactly.

`ConfirmedIdentity` can only be constructed by `confirm_identity`, which refuses unless
name, date of birth, and address were all explicitly confirmed or corrected. An
unconfirmed, partial, revoked, or failed OCR result therefore has no path to a write
(AC1, AC3): there is no constructor that accepts a missing field.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import OpenEmrRequestError

_DEMOGRAPHICS_PATH = "/portal/patient/demographics"


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


class OpenEmrDemographicsAdapter:
    """Writes a confirmed identity for the caller's own bound patient (TICK-042).

    Takes only the caller's already-delegated bearer token; this adapter never stores,
    caches, or otherwise retains it, and never resolves "the logged-in patient" itself
    -- OpenEMR does that server-side from the token on every call.
    """

    def __init__(self, settings: OpenEmrPortalSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def write_confirmed_demographics(
        self, access_token: str, identity: ConfirmedIdentity
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
                f"{self._settings.portal_base_url}{_DEMOGRAPHICS_PATH}",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OpenEmrRequestError("writing confirmed demographics to OpenEMR failed") from exc
        if response.status_code != 200:
            raise OpenEmrRequestError("writing confirmed demographics to OpenEMR failed")
