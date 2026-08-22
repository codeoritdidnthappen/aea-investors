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

An address on its own is a second, narrower confirmed unit (`ConfirmedAddress`, built
only by `confirm_address`, TICK-049) so a patient who only wants to move house does not
have to re-confirm a name and date of birth that are not changing. Both units keep the
address structured all the way to the wire: OpenEMR's `patient_data` has its own
`street`, `street_line_2`, `city`, `state`, and `postal_code` columns, so flattening a
validated `Address` into one `street` line (as this module did before TICK-049) threw
away structure OpenEMR itself models.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.onboarding.fields import Address, FieldValidationError, validate_address
from ai_server.openemr.adapter import OpenEmrRequestError

_DEMOGRAPHICS_PATH = "/portal/patient/demographics"


class IdentityNotConfirmedError(Exception):
    """Raised when a field required for the attempted write was not explicitly confirmed."""


@dataclass(frozen=True)
class ConfirmedAddress:
    """Only an explicitly confirmed, fully validated address; every component non-empty.

    Kept component-wise rather than as one formatted line so each part can land in its
    own OpenEMR column (AC2). `street2` is the single genuinely optional component --
    most addresses have no unit/apartment line, and inventing one would fabricate a
    value the patient never gave (FR-23).
    """

    street1: str
    city: str
    state: str
    zip_code: str
    street2: str | None = None


@dataclass(frozen=True)
class ConfirmedIdentity:
    """Only a fully confirmed identity; every field is non-empty.

    `given_name` and `family_name` are kept separate rather than one `name` string:
    OpenEMR's write endpoint takes them as separate fields, and no join-then-split
    scheme can recover an internally-spaced family name (e.g. "Van Der Berg") from a
    joined string without guessing. `family_name` must be non-empty like every other
    field here: OpenEMR's own `PatientValidator` rejects an empty last name outright
    (`NotEmpty::EMPTY_VALUE`), so a mononym cannot be written regardless of what this
    class would otherwise accept, and fabricating a placeholder surname would violate
    this system's confirmed-only, no-fabricated-values guarantee (FR-23) -- see
    `tickets/TICK-043-fix-mononym-demographics-validation.md`.
    """

    given_name: str
    family_name: str
    date_of_birth: str
    address: ConfirmedAddress


def confirm_address(address: Address | None) -> ConfirmedAddress:
    """Build a write-eligible address only once it is confirmed and fully valid (AC5).

    `address` is the `Address` the patient explicitly confirmed or corrected. Re-running
    `fields.validate_address` here rather than trusting the dataclass keeps one set of
    address rules (`ONBOARDING_CONTRACT.md` row 5: real two-letter state/territory code,
    five- or nine-digit ZIP) and puts them at the write boundary itself, so a
    hand-constructed or partially-validated `Address` -- an invalid state, a malformed
    ZIP, a blank city -- is refused before any request is made, exactly as `None` is.
    """
    if address is None:
        raise IdentityNotConfirmedError("identity fields not confirmed: address")
    try:
        validated = validate_address(
            {
                "street1": address.street1,
                "street2": address.street2,
                "city": address.city,
                "state": address.state,
                "zip_code": address.zip_code,
            }
        )
    except FieldValidationError as exc:
        raise IdentityNotConfirmedError(
            f"address is not fully validated: {'; '.join(exc.details)}"
        ) from exc
    return ConfirmedAddress(
        street1=validated.street1,
        city=validated.city,
        state=validated.state,
        zip_code=validated.zip_code,
        # `validate_address` returns `""` for a whitespace-only second line; collapse
        # that to `None` so "no second line" has exactly one representation here.
        street2=validated.street2 or None,
    )


def confirm_identity(
    given_name: str | None,
    family_name: str | None,
    date_of_birth: str | None,
    address: Address | None,
) -> ConfirmedIdentity:
    """Build a write-eligible identity only once every required field is confirmed (AC1).

    Each argument is the value the patient explicitly confirmed or corrected, never
    a raw OCR value passed through automatically. `None` or blank covers every
    no-write case in one path: never extracted, extracted but not yet confirmed,
    revoked, or failed OCR (AC3) - all refuse identically instead of writing a
    partial record.
    """
    fields = {
        "given_name": given_name,
        "family_name": family_name,
        "date_of_birth": date_of_birth,
        "address": address,
    }
    missing = [field for field, value in fields.items() if not value]
    if missing:
        raise IdentityNotConfirmedError(f"identity fields not confirmed: {', '.join(missing)}")
    assert given_name and family_name and date_of_birth  # narrows for the type checker
    return ConfirmedIdentity(
        given_name=given_name,
        family_name=family_name,
        date_of_birth=date_of_birth,
        address=confirm_address(address),
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
        await self._put(
            access_token,
            {
                "fname": identity.given_name,
                "lname": identity.family_name,
                "DOB": identity.date_of_birth,
                **_address_body(identity.address),
            },
        )

    async def write_confirmed_address(self, access_token: str, address: ConfirmedAddress) -> None:
        """Write only the confirmed address, leaving name and date of birth untouched.

        The body carries address columns and nothing else, so `PatientService::update()`
        builds an `UPDATE` that names only those columns -- an omitted field is never
        written, and therefore never blanked (AC1). This is the address-only path
        TICK-050 needs; it takes a `ConfirmedAddress`, so an unconfirmed or only
        partially validated address still has no route to a write (AC5).
        """
        await self._put(access_token, _address_body(address))

    async def _put(self, access_token: str, body: dict[str, str]) -> None:
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


def _address_body(address: ConfirmedAddress) -> dict[str, str]:
    """Map a confirmed address onto OpenEMR's own `patient_data` address columns (AC2).

    Keys are the real column names `PatientService::update()` writes through
    (`BaseService::buildUpdateColumns` drops any key that is not a column), matching how
    this body already names `fname`/`lname`/`DOB` rather than inventing a wire vocabulary.

    An address is written as one whole unit, `street_line_2` included and sent empty when
    the patient gave no second line. Omitting it instead would leave the previous
    address's apartment/unit line sitting on the record -- verified against a real
    OpenEMR in `evidence/TICK-049/ADDRESS_WRITE_EVIDENCE.md` -- so the stored address
    would no longer be the address the patient confirmed. Only `street_line_2` is ever
    sent empty; every other component is required to be non-empty by `confirm_address`,
    and the endpoint refuses an empty value for any of them.
    """
    return {
        "street": address.street1,
        "street_line_2": address.street2 or "",
        "city": address.city,
        "state": address.state,
        "postal_code": address.zip_code,
    }
