"""Synthetic integration tests for confirmed-only demographic writes (TICK-016, TICK-042).

TICK-049 adds the two properties `evidence/TICK-049/ADDRESS_WRITE_EVIDENCE.md` proves
live against a real OpenEMR 8.3.0 + MariaDB stack: an address-only write carries address
columns and nothing else (so name and date of birth cannot be blanked), and every address
component travels in its own OpenEMR column rather than one concatenated `street` line.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_server.ocr.service import ExtractedIdentity
from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.onboarding.fields import Address
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.openemr.demographics import (
    ConfirmedAddress,
    ConfirmedIdentity,
    IdentityNotConfirmedError,
    OpenEmrDemographicsAdapter,
    confirm_address,
    confirm_identity,
)

PORTAL_BASE_URL = "https://openemr.test/apis/default"
DEMOGRAPHICS_URL = f"{PORTAL_BASE_URL}/portal/patient/demographics"

ADDRESS = Address(street1="100 Maple Avenue", city="Springfield", state="IL", zip_code="62704")


def settings() -> OpenEmrPortalSettings:
    return OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)


def adapter_with(handler: httpx.MockTransport) -> OpenEmrDemographicsAdapter:
    client = httpx.AsyncClient(transport=handler)
    return OpenEmrDemographicsAdapter(settings(), client)


def run(coroutine):
    return asyncio.run(coroutine)


def recording_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "updated"})

    return httpx.MockTransport(handler)


def test_ac1_confirming_every_field_yields_a_writable_identity() -> None:
    identity = confirm_identity("Avery", "Alden", "1990-01-01", ADDRESS)

    assert identity == ConfirmedIdentity(
        given_name="Avery",
        family_name="Alden",
        date_of_birth="1990-01-01",
        address=ConfirmedAddress(
            street1="100 Maple Avenue", city="Springfield", state="IL", zip_code="62704"
        ),
    )


@pytest.mark.parametrize(
    "given_name,date_of_birth,address",
    [
        (None, "1990-01-01", ADDRESS),
        ("Avery", None, ADDRESS),
        ("Avery", "1990-01-01", None),
        (None, None, None),
        ("", "1990-01-01", ADDRESS),
    ],
)
def test_ac1_ac3_any_unconfirmed_or_partial_field_refuses_before_a_write_is_possible(
    given_name: str | None, date_of_birth: str | None, address: Address | None
) -> None:
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(given_name, "Alden", date_of_birth, address)


def test_ac3_a_failed_ocr_extraction_never_becomes_writable() -> None:
    """A failed/unavailable Tesseract run leaves every field `None` (ocr/service.py)."""
    failed = ExtractedIdentity()

    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(failed.name, "Alden", failed.date_of_birth, None)


def test_ac3_a_partial_ocr_extraction_never_becomes_writable() -> None:
    """OCR only ever yields an unstructured address string, never a validated, confirmed
    `Address`, so a raw extraction has no path to a write on the address side either."""
    partial = ExtractedIdentity(name="Avery", date_of_birth=None, address="100 Maple Avenue")

    assert not isinstance(partial.address, Address)
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(partial.name, "Alden", partial.date_of_birth, None)


def test_ac3_a_revoked_upload_never_becomes_writable() -> None:
    """`OcrService.revoke` purges the store; `identity()` then returns `None` (FR-23)."""
    revoked_identity = None

    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(revoked_identity, revoked_identity, revoked_identity, revoked_identity)


def test_ac2_writes_only_the_confirmed_name_dob_and_address_to_the_mapped_endpoint() -> None:
    captured: list[httpx.Request] = []
    identity = confirm_identity("Avery", "Alden", "1990-01-01", ADDRESS)

    run(
        adapter_with(recording_transport(captured)).write_confirmed_demographics(
            "synthetic-access-token", identity
        )
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "PUT"
    assert str(request.url) == DEMOGRAPHICS_URL
    assert request.headers["authorization"] == "Bearer synthetic-access-token"
    body = json.loads(request.content)
    assert body == {
        "fname": "Avery",
        "lname": "Alden",
        "DOB": "1990-01-01",
        "street": "100 Maple Avenue",
        "street_line_2": "",
        "city": "Springfield",
        "state": "IL",
        "postal_code": "62704",
    }


def test_tick_049_address_components_are_never_concatenated_into_one_street_line() -> None:
    """AC2: `street` carries line 1 only -- city/state/ZIP have their own OpenEMR columns
    (`patient_data.city`, `.state`, `.postal_code`), and a second street line goes to
    `street_line_2`, so the stored row matches the components the patient confirmed."""
    captured: list[httpx.Request] = []
    address = confirm_address(
        Address(
            street1="42 Oak St",
            street2="Apt 4B",
            city="Springfield",
            state="IL",
            zip_code="62704",
        )
    )

    run(adapter_with(recording_transport(captured)).write_confirmed_address("token", address))

    body = json.loads(captured[0].content)
    assert body == {
        "street": "42 Oak St",
        "street_line_2": "Apt 4B",
        "city": "Springfield",
        "state": "IL",
        "postal_code": "62704",
    }
    assert "42 Oak St, Springfield" not in body["street"]


def test_tick_049_an_address_only_write_carries_no_name_or_date_of_birth() -> None:
    """AC1: the request names address columns only. `PatientService::update()` builds its
    `UPDATE` from the keys it is given, so an omitted `fname`/`lname`/`DOB` is left alone
    rather than blanked -- there is nothing in this body that could overwrite them."""
    captured: list[httpx.Request] = []
    address = confirm_address(ADDRESS)

    run(adapter_with(recording_transport(captured)).write_confirmed_address("token", address))

    assert len(captured) == 1
    assert str(captured[0].url) == DEMOGRAPHICS_URL
    body = json.loads(captured[0].content)
    assert set(body) == {"street", "street_line_2", "city", "state", "postal_code"}
    assert not {"fname", "lname", "DOB"} & set(body)


def test_tick_049_a_new_address_without_a_second_line_clears_the_previous_one() -> None:
    """The address is written as one unit: `street_line_2` is sent empty rather than
    omitted, because an omitted field is left alone by `PatientService::update()` and the
    previous address's apartment line would survive onto the new address -- proved
    against a real OpenEMR in `evidence/TICK-049/ADDRESS_WRITE_EVIDENCE.md`. Nothing is
    fabricated here: empty means the patient gave no second line."""
    captured: list[httpx.Request] = []

    run(
        adapter_with(recording_transport(captured)).write_confirmed_address(
            "token", confirm_address(ADDRESS)
        )
    )

    assert confirm_address(ADDRESS).street2 is None
    assert json.loads(captured[0].content)["street_line_2"] == ""


@pytest.mark.parametrize(
    "address",
    [
        None,
        Address(street1="42 Oak St", city="Springfield", state="Illinois", zip_code="62704"),
        Address(street1="42 Oak St", city="Springfield", state="ZZ", zip_code="62704"),
        Address(street1="42 Oak St", city="Springfield", state="IL", zip_code="62"),
        Address(street1="42 Oak St", city="Springfield", state="IL", zip_code="not-a-zip"),
        Address(street1="", city="Springfield", state="IL", zip_code="62704"),
        Address(street1="42 Oak St", city="   ", state="IL", zip_code="62704"),
    ],
)
def test_tick_049_ac5_an_unconfirmed_or_invalid_address_is_refused_before_any_write(
    address: Address | None,
) -> None:
    """AC5: the confirmed-only rule holds for the address-only path too. Every one of
    these refuses at `confirm_address`, so no `ConfirmedAddress` -- and therefore no
    request -- can exist for a partially validated address."""
    with pytest.raises(IdentityNotConfirmedError):
        confirm_address(address)
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity("Avery", "Alden", "1990-01-01", address)


def test_tick_049_a_confirmed_address_is_normalized_the_same_way_validation_normalizes() -> None:
    """One set of address rules, applied at the write boundary too: the state is
    upper-cased and surrounding whitespace stripped exactly as `validate_address` does,
    so a confirmed address cannot reach OpenEMR in a shape validation would have fixed."""
    address = confirm_address(
        Address(
            street1="  42 Oak St ",
            street2="  ",
            city=" Springfield ",
            state="il",
            zip_code="62704-1234",
        )
    )

    assert address == ConfirmedAddress(
        street1="42 Oak St",
        city="Springfield",
        state="IL",
        zip_code="62704-1234",
        street2=None,
    )


def test_tick_043_a_mononym_is_refused_not_written_with_a_fabricated_surname() -> None:
    """A missing family name is refused, like every other required field (TICK-043):
    OpenEMR's own PatientValidator rejects an empty last name outright regardless, and
    writing a placeholder surname would fabricate a value this system never confirmed
    (FR-23) -- there is no code path from "no family name" to a write."""
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity("Cher", None, "1990-01-01", ADDRESS)
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity("Cher", "", "1990-01-01", ADDRESS)


def test_ac2_a_multi_word_family_name_is_never_split() -> None:
    """given_name/family_name are carried separately end to end, never joined then
    re-split -- a join+split round trip cannot recover a multi-word family name
    ("Van Der Berg") without guessing which word is the family name."""
    captured: list[httpx.Request] = []
    identity = confirm_identity("Avery", "Van Der Berg", "1990-01-01", ADDRESS)

    run(adapter_with(recording_transport(captured)).write_confirmed_demographics("token", identity))

    body = json.loads(captured[0].content)
    assert body["fname"] == "Avery"
    assert body["lname"] == "Van Der Berg"


def test_ac2_a_non_200_response_fails_explicitly_with_no_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    identity = confirm_identity("Avery", "Alden", "1990-01-01", ADDRESS)

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).write_confirmed_demographics(
            "token", identity
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_tick_049_an_address_only_write_fails_explicitly_on_a_non_200_too() -> None:
    """The address-only path must not report success when OpenEMR refused the write
    (FR-16's "never claims success unless OpenEMR confirms the write")."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "validation failed"})

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).write_confirmed_address(
            "token", confirm_address(ADDRESS)
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_ac2_a_transport_failure_fails_explicitly_with_no_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    identity = confirm_identity("Avery", "Alden", "1990-01-01", ADDRESS)

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).write_confirmed_demographics(
            "token", identity
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())
