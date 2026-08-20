"""Static checks for the aeai-portal-chat OpenEMR module (TICK-012).

Docker itself is not available in every CI runner (see test_local_deployment.py), and
no browser-automation tool is available in this repository's execution environment
(evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md, "Method note"). These checks therefore
validate the committed module source and its wiring into the local demo topology,
rather than driving a real browser against a running OpenEMR container.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO_ROOT / "openemr_modules" / "aeai-portal-chat"
_MODULE_DIRECTORY_NAME = "aeai-portal-chat"


def _bootstrap_text() -> str:
    return (_MODULE_DIR / "openemr.bootstrap.php").read_text(encoding="utf-8")


def _controller_text() -> str:
    return (_MODULE_DIR / "src" / "Controller" / "PortalChatController.php").read_text(
        encoding="utf-8"
    )


def _compose_text() -> str:
    return (_REPO_ROOT / "deploy" / "local" / "docker-compose.yml").read_text(encoding="utf-8")


def test_bootstrap_registers_the_module_namespace_and_subscribes_on_load() -> None:
    bootstrap = _bootstrap_text()

    assert "registerNamespaceIfNotExists('AeaiPortalChat\\\\'" in bootstrap
    assert "subscribeToEvents()" in bootstrap


def test_controller_hooks_only_the_authenticated_patient_portal_render_event() -> None:
    controller = _controller_text()

    # The *patient-portal* RenderEvent (selected by TICK-002), not the unrelated
    # Main\Tabs\RenderEvent some bundled OpenEMR modules also use.
    assert "use OpenEMR\\Events\\PatientPortal\\RenderEvent;" in controller
    assert "RenderEvent::EVENT_SECTION_RENDER_POST" in controller
    assert "RenderEvent::EVENT_DASHBOARD_INJECT_CARD" in controller
    # This is the whole logged-out guarantee (FR-1): no event outside the
    # patient-portal RenderEvent family, and no unauthenticated route or endpoint, is
    # registered anywhere in the module. Both listeners fire only from portal/home.php
    # (evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md), which never renders for a logged-out
    # visitor.
    assert "addListener" in controller
    assert controller.count("addListener") == 2


def test_controller_dashboard_tile_matches_the_existing_nav_icon_pattern() -> None:
    controller = _controller_text()

    # TICK-032: a launcher tile in the dashboard grid, using the same markup
    # contract as templates/portal/partial/_nav_icon.html.twig (the include every
    # other dashboard tile -- Clinical Documents, Appointments, etc. -- uses), so it
    # is visible without scrolling and behaves like its siblings.
    assert 'data-toggle="collapse"' in controller
    assert 'data-parent="#cardgroup"' in controller
    assert "href=\"#' . self::CARD_ID . '\"" in controller
    assert "CARD_ID = 'aeai-portal-chat'" in controller


def test_controller_panel_is_a_collapsible_accordion_card() -> None:
    controller = _controller_text()

    # The chat panel itself must be one more `.collapse` card sharing `#cardgroup`
    # with every other portal feature's panel (e.g. `#downloadcard`), not a bare
    # always-visible section appended after the dashboard.
    assert 'id="\' . self::CARD_ID . \'" class="card collapse overflow-auto"' in controller
    assert 'data-parent="#cardgroup">\'' in controller


def test_controller_renders_a_single_iframe_with_no_openemr_api_reference() -> None:
    controller = _controller_text()

    assert "<iframe" in controller
    for openemr_marker in ("/apis/", "/oauth2/", "fhir", "Bearer", "fetch(", "XMLHttpRequest"):
        assert openemr_marker not in controller, f"module source references {openemr_marker!r}"


def test_controller_iframe_src_is_configurable_with_the_local_chat_launch_default() -> None:
    controller = _controller_text()

    assert "getenv('AEAI_PORTAL_CHAT_URL')" in controller
    assert "https://chat.localhost/oauth/launch" in controller


def test_module_directory_name_matches_the_required_registration_contract() -> None:
    # The OpenEMR module loader requires modules.mod_directory to equal the directory
    # name under interface/modules/custom_modules/ (evidence/TICK-002, "Database
    # registration contract"). Enforce that the directory we ship is the one README
    # and docker-compose.yml both reference.
    assert _MODULE_DIR.name == _MODULE_DIRECTORY_NAME
    assert (_MODULE_DIR / "openemr.bootstrap.php").is_file()
    assert (_MODULE_DIR / "info.txt").is_file()


def test_docker_compose_mounts_the_module_read_only_into_custom_modules() -> None:
    compose = _compose_text()

    assert (
        f"../../openemr_modules/{_MODULE_DIRECTORY_NAME}:"
        f"/var/www/localhost/htdocs/openemr/interface/modules/custom_modules/"
        f"{_MODULE_DIRECTORY_NAME}:ro" in compose
    )


def test_docker_compose_passes_a_configurable_chat_url_to_openemr() -> None:
    compose = _compose_text()

    assert (
        "AEAI_PORTAL_CHAT_URL: ${AEAI_PORTAL_CHAT_URL:-https://chat.localhost/oauth/launch}"
        in compose
    )


def test_env_example_documents_the_chat_url_override() -> None:
    env_example = (_REPO_ROOT / "deploy" / "local" / ".env.example").read_text(encoding="utf-8")

    assert "AEAI_PORTAL_CHAT_URL=https://chat.localhost/oauth/launch" in env_example


def test_readme_documents_registering_and_enabling_the_module() -> None:
    readme = (_REPO_ROOT / "deploy" / "local" / "README.md").read_text(encoding="utf-8")

    assert "aeai-portal-chat" in readme
    assert "Manage Modules" in readme
