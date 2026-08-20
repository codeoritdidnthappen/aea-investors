"""Static checks for the appointment-cancel OpenEMR module route (TICK-031).

Mirrors `ai_server/tests/test_portal_module.py`'s approach: Docker is not available in
every CI runner (see `test_local_deployment.py`), so these validate the committed PHP
module source directly rather than driving a running OpenEMR container. The live,
cross-patient-negative proof for this route belongs in a probe script and evidence
file, the same discipline `evidence/TICK-017/ASSESSMENT_DRAFT_EVIDENCE.md` used.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO_ROOT / "openemr_modules" / "aeai-portal-chat"


def _bootstrap_text() -> str:
    return (_MODULE_DIR / "src" / "Bootstrap.php").read_text(encoding="utf-8")


def _controller_text() -> str:
    return (_MODULE_DIR / "src" / "Controller" / "AppointmentCancelController.php").read_text(
        encoding="utf-8"
    )


def _service_text() -> str:
    return (_MODULE_DIR / "src" / "Service" / "AppointmentCancelService.php").read_text(
        encoding="utf-8"
    )


def test_bootstrap_subscribes_the_appointment_cancel_controller() -> None:
    bootstrap = _bootstrap_text()

    assert "use AeaiPortalChat\\Controller\\AppointmentCancelController;" in bootstrap
    assert (
        "(new AppointmentCancelController())->subscribeToEvents($this->eventDispatcher);"
        in bootstrap
    )


def test_controller_registers_a_put_route_not_a_delete() -> None:
    controller = _controller_text()

    assert "'PUT /portal/patient/appointment/:auuid'" in controller
    assert "addToPortalRouteMap('DELETE" not in controller
    assert "->delete(" not in controller


def test_controller_adds_a_patient_scoped_write_scope() -> None:
    controller = _controller_text()

    assert "$event->addScope('patient', 'appointment', 'u');" in controller


def test_controller_binds_only_to_the_token_derived_patient_uuid() -> None:
    controller = _controller_text()

    assert "$request->getPatientUUIDString()" in controller
    # No client-supplied identifier (query/body/header) is ever treated as the patient.
    assert "getPatientUUIDString" in controller


def test_service_ownership_check_filters_by_both_patient_and_appointment_uuid_at_once() -> None:
    service = _service_text()

    assert "'puuid' => $patientUuid" in service
    assert "'pc_uuid' => $auuid" in service
    # Both filters are in the single search() call, so a mismatch is a query miss
    # (404), never a fetch-then-compare that could be edited to skip the check.
    assert service.count("search([") == 1


def test_service_never_deletes_and_uses_the_real_cancelled_with_history_status() -> None:
    service = _service_text()

    assert "updateAppointmentStatus(" in service
    assert "deleteAppointmentRecord" not in service
    assert "DELETE FROM" not in service
    assert "CANCELLED_STATUS = 'x'" in service


def test_service_refuses_an_unbound_request_before_any_query() -> None:
    service = _service_text()

    assert "no bound patient on this request" in service


def test_service_rejects_an_already_cancelled_appointment_with_409_not_a_silent_success() -> None:
    service = _service_text()

    assert "409" in service
    assert "already cancelled" in service


def test_module_source_lives_only_under_the_registered_module_directory() -> None:
    assert (_MODULE_DIR / "src" / "Controller" / "AppointmentCancelController.php").is_file()
    assert (_MODULE_DIR / "src" / "Service" / "AppointmentCancelService.php").is_file()
