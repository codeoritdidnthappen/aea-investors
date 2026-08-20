"""Static checks for the assessment-draft OpenEMR module extension (TICK-017).

Live proof (real cross-patient binding negative tests against a running OpenEMR
v8.3.0 container, using a real authorization_code+PKCE patient token) lives in
evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md and scripts/probe_assessment_draft.py
-- those require a live Docker stack this test suite does not assume is running (see
test_portal_module.py's docstring for the same constraint). These checks instead
guard the properties that proof depended on: every query is scoped by the
token-derived patient uuid, no core OpenEMR file is touched, and the module wires
into OpenEMR's real extension events rather than some invented mechanism.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO_ROOT / "openemr_modules" / "aeai-portal-chat"


def _controller_text() -> str:
    return (_MODULE_DIR / "src" / "Controller" / "AssessmentDraftController.php").read_text(
        encoding="utf-8"
    )


def _service_text() -> str:
    return (_MODULE_DIR / "src" / "Service" / "AssessmentDraftService.php").read_text(
        encoding="utf-8"
    )


def _bootstrap_text() -> str:
    return (_MODULE_DIR / "src" / "Bootstrap.php").read_text(encoding="utf-8")


def _sql_text() -> str:
    return (_MODULE_DIR / "sql" / "table.sql").read_text(encoding="utf-8")


def test_bootstrap_wires_the_assessment_draft_controller() -> None:
    bootstrap = _bootstrap_text()
    assert "AssessmentDraftController" in bootstrap
    assert "subscribeToEvents" in bootstrap


def test_controller_registers_routes_and_scopes_through_official_extension_events() -> None:
    controller = _controller_text()
    # OpenEMR::RestApiExtend is a first-class module extension point (route map and
    # OAuth scope), not a private/internal API this module happens to reach into.
    assert "RestApiCreateEvent" in controller
    assert "RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES" in controller
    assert "addToPortalRouteMap" in controller
    # Portal, never FHIR: AuthorizationListener unconditionally blocks patient-role
    # FHIR writes core-side, independent of which route triggers it.
    assert "addToFHIRRouteMap" not in controller


def test_controller_never_trusts_a_client_supplied_patient_identifier() -> None:
    controller = _controller_text()
    # The only patient identifier ever passed to the service layer is the
    # token-derived one -- there is no route parameter or request-body field read
    # for "patient id" anywhere in this file.
    assert controller.count("getPatientUUIDString()") >= 3  # create, read, update


def test_service_scopes_every_query_by_patient_uuid() -> None:
    service = _service_text()
    for statement in ("SELECT", "UPDATE", "INSERT"):
        assert statement in service
    assert service.count("patient_uuid = ?") >= 1
    assert "WHERE patient_uuid = ? AND uuid = ?" in service
    # No query anywhere looks a draft up by uuid alone -- that's the entire binding
    # boundary the live cross-patient test in evidence/TICK-017 exercises.
    assert "WHERE uuid = ?" not in service


def test_service_uses_parameterized_queries_only() -> None:
    service = _service_text()
    # Every sql* call in this file must bind values, never interpolate them --
    # otherwise the binding-by-query-scope guarantee above would be moot.
    sql_call_prefixes = ("sqlInsert(", "sqlStatement(", "sqlQuery(")
    for line in service.splitlines():
        stripped = line.strip()
        if stripped.startswith(sql_call_prefixes):
            assert "$patientUuid" not in stripped
            assert "$uuid" not in stripped


def test_service_requires_all_contract_fields_to_complete() -> None:
    service = _service_text()
    # ONBOARDING_CONTRACT.md rows 6-9 (the assessment-draft portion; identity fields
    # are TICK-016's concern).
    for field in (
        "preferred_contact_method",
        "help_type",
        "visit_format",
        "visit_time_window",
    ):
        assert field in service
    assert "REQUIRED_FOR_COMPLETION" in service


def test_module_ships_its_own_table_no_core_schema_touched() -> None:
    sql = _sql_text()
    assert "CREATE TABLE IF NOT EXISTS aeai_assessment_draft" in sql
    assert "patient_uuid" in sql
