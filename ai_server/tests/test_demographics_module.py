"""Static checks for the demographics-write OpenEMR module extension (TICK-042, TICK-049).

Live proof (a real address-only write against a running OpenEMR v8.3.0 container and its
MariaDB, read back column by column) lives in
`evidence/TICK-049/ADDRESS_WRITE_EVIDENCE.md`; that needs a live Docker stack this suite
does not assume is running, the same constraint `test_assessment_draft_module.py` and
`test_portal_module.py` document. These checks instead guard the properties that proof
depends on: every writable field is optional so a partial write is possible, the address
is written as separate OpenEMR columns, an unrecognised or empty body is refused rather
than reported as a successful no-op, and the write still goes through OpenEMR's own
`PatientService` rather than SQL (FR-17).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO_ROOT / "openemr_modules" / "aeai-portal-chat"

# The `patient_data` columns the route writes, verified against the running container's
# own schema (`SHOW COLUMNS FROM patient_data`) rather than guessed: OpenEMR's
# `BaseService::buildUpdateColumns` silently drops any key that is not a real column, so
# a wrong name here would look like success and write nothing.
_ADDRESS_COLUMNS = ("street", "street_line_2", "city", "state", "postal_code")


def _service_text() -> str:
    return (_MODULE_DIR / "src" / "Service" / "PatientDemographicsUpdateService.php").read_text(
        encoding="utf-8"
    )


def _controller_text() -> str:
    return (_MODULE_DIR / "src" / "Controller" / "PatientDemographicsController.php").read_text(
        encoding="utf-8"
    )


def test_the_service_accepts_every_structured_address_column() -> None:
    service = _service_text()
    for column in _ADDRESS_COLUMNS:
        assert f"'{column}'" in service


def test_no_field_is_required_so_a_partial_address_only_write_is_possible() -> None:
    """AC1/AC3: the pre-TICK-049 `REQUIRED_STRING_FIELDS` loop demanded fname, lname, DOB
    and street on every request, which made an address-only update impossible."""
    service = _service_text()
    assert "REQUIRED_STRING_FIELDS" not in service
    assert "WRITABLE_STRING_FIELDS" in service


def test_an_unrecognised_field_is_refused_rather_than_silently_dropped() -> None:
    service = _service_text()
    assert "is not a writable demographics field" in service
    assert "in_array($field, self::WRITABLE_STRING_FIELDS, true)" in service


def test_only_the_second_street_line_may_be_sent_empty_to_clear_it() -> None:
    """The address is written as a unit, so moving to an address with no apartment line
    must clear the old one; an empty name or date of birth stays a 400."""
    service = _service_text()
    assert "private const CLEARABLE_STRING_FIELDS = ['street_line_2'];" in service
    assert "if ($value === '' && !in_array($field, self::CLEARABLE_STRING_FIELDS, true))" in service


def test_a_body_with_no_recognised_field_is_refused_not_reported_as_updated() -> None:
    """AC3. `ParsesJsonRequestBody::parseJsonBody()` returns `[]` for a missing or empty
    body, so without this check an empty request would write nothing and answer 200."""
    service = _service_text()
    assert "if (empty($fields))" in service
    assert "at least one demographics field is required" in service
    # The guard is only load-bearing because `update()` returns that error response
    # straight back instead of continuing to the write and the 200.
    assert "if ($fields instanceof JsonResponse) {\n            return $fields;" in service


def test_the_write_still_goes_through_openemrs_own_patient_service_not_sql() -> None:
    """FR-17: every write uses existing OpenEMR business logic, never direct database
    access -- which is also what keeps it inside OpenEMR's own audited write path."""
    service = _service_text()
    assert "(new PatientService())->update($patientUuid, $fields)" in service
    for statement in ("SELECT", "UPDATE ", "INSERT", "sqlStatement", "sqlQuery"):
        assert statement not in service


def test_the_service_still_refuses_a_request_with_no_token_bound_patient() -> None:
    service = _service_text()
    assert "UuidRegistry::isValidStringUUID($patientUuid)" in service
    assert "'no bound patient on this request'" in service


def test_the_controller_never_trusts_a_client_supplied_patient_identifier() -> None:
    """Unchanged by TICK-049 and load-bearing for it: a partial write is only safe
    because the target patient still comes from the bearer token, never the body."""
    controller = _controller_text()
    assert "getPatientUUIDString()" in controller
    assert "addToPortalRouteMap" in controller
    assert "addToFHIRRouteMap" not in controller
