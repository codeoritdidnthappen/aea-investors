"""Synthetic integration tests for confirmed-only demographic writes (TICK-016, TICK-042)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_server.ocr.service import ExtractedIdentity
from ai_server.onboarding.draft_client import OpenEmrPortalSettings
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.openemr.demographics import (
    ConfirmedIdentity,
    IdentityNotConfirmedError,
    OpenEmrDemographicsAdapter,
    confirm_identity,
)

PORTAL_BASE_URL = "https://openemr.test/apis/default"
DEMOGRAPHICS_URL = f"{PORTAL_BASE_URL}/portal/patient/demographics"


def settings() -> OpenEmrPortalSettings:
    return OpenEmrPortalSettings(portal_base_url=PORTAL_BASE_URL)


def adapter_with(handler: httpx.MockTransport) -> OpenEmrDemographicsAdapter:
    client = httpx.AsyncClient(transport=handler)
    return OpenEmrDemographicsAdapter(settings(), client)


def run(coroutine):
    return asyncio.run(coroutine)


def test_ac1_confirming_every_field_yields_a_writable_identity() -> None:
    identity = confirm_identity("Avery", "Alden", "1990-01-01", "100 Maple Avenue")

    assert identity == ConfirmedIdentity(
        given_name="Avery",
        family_name="Alden",
        date_of_birth="1990-01-01",
        address="100 Maple Avenue",
    )


@pytest.mark.parametrize(
    "given_name,date_of_birth,address",
    [
        (None, "1990-01-01", "100 Maple Avenue"),
        ("Avery", None, "100 Maple Avenue"),
        ("Avery", "1990-01-01", None),
        (None, None, None),
        ("", "1990-01-01", "100 Maple Avenue"),
    ],
)
def test_ac1_ac3_any_unconfirmed_or_partial_field_refuses_before_a_write_is_possible(
    given_name: str | None, date_of_birth: str | None, address: str | None
) -> None:
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(given_name, "Alden", date_of_birth, address)


def test_ac3_a_failed_ocr_extraction_never_becomes_writable() -> None:
    """A failed/unavailable Tesseract run leaves every field `None` (ocr/service.py)."""
    failed = ExtractedIdentity()

    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(failed.name, "Alden", failed.date_of_birth, failed.address)


def test_ac3_a_partial_ocr_extraction_never_becomes_writable() -> None:
    partial = ExtractedIdentity(name="Avery", date_of_birth=None, address="100 Maple Avenue")

    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(partial.name, "Alden", partial.date_of_birth, partial.address)


def test_ac3_a_revoked_upload_never_becomes_writable() -> None:
    """`OcrService.revoke` purges the store; `identity()` then returns `None` (FR-23)."""
    revoked_identity = None

    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity(revoked_identity, revoked_identity, revoked_identity, revoked_identity)


def test_ac2_writes_only_the_confirmed_name_dob_and_address_to_the_mapped_endpoint() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "updated"})

    identity = confirm_identity("Avery", "Alden", "1990-01-01", "100 Maple Avenue")

    run(
        adapter_with(httpx.MockTransport(handler)).write_confirmed_demographics(
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
    }


def test_tick_043_a_mononym_is_refused_not_written_with_a_fabricated_surname() -> None:
    """A missing family name is refused, like every other required field (TICK-043):
    OpenEMR's own PatientValidator rejects an empty last name outright regardless, and
    writing a placeholder surname would fabricate a value this system never confirmed
    (FR-23) -- there is no code path from "no family name" to a write."""
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity("Cher", None, "1990-01-01", "100 Maple Avenue")
    with pytest.raises(IdentityNotConfirmedError):
        confirm_identity("Cher", "", "1990-01-01", "100 Maple Avenue")


def test_ac2_a_multi_word_family_name_is_never_split() -> None:
    """given_name/family_name are carried separately end to end, never joined then
    re-split -- a join+split round trip cannot recover a multi-word family name
    ("Van Der Berg") without guessing which word is the family name."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "updated"})

    identity = confirm_identity("Avery", "Van Der Berg", "1990-01-01", "100 Maple Avenue")

    run(adapter_with(httpx.MockTransport(handler)).write_confirmed_demographics("token", identity))

    body = json.loads(captured[0].content)
    assert body["fname"] == "Avery"
    assert body["lname"] == "Van Der Berg"


def test_ac2_a_non_200_response_fails_explicitly_with_no_fallback() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    identity = confirm_identity("Avery", "Alden", "1990-01-01", "100 Maple Avenue")

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).write_confirmed_demographics(
            "token", identity
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())


def test_ac2_a_transport_failure_fails_explicitly_with_no_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    identity = confirm_identity("Avery", "Alden", "1990-01-01", "100 Maple Avenue")

    async def scenario() -> None:
        await adapter_with(httpx.MockTransport(handler)).write_confirmed_demographics(
            "token", identity
        )

    with pytest.raises(OpenEmrRequestError):
        run(scenario())
