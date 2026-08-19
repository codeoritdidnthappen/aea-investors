"""Static checks for the reproducible local demo topology (TICK-022).

Docker itself is not available in every CI runner, so these checks validate the
committed configuration rather than actually bringing up containers: the pinned
OpenEMR release, separate OpenEMR/MariaDB and AI-server services, a persistent
AI-session volume, and secrets kept out of source control.
"""

from __future__ import annotations

from pathlib import Path

_DEPLOY_LOCAL = Path(__file__).resolve().parents[2] / "deploy" / "local"
_GITIGNORE = Path(__file__).resolve().parents[2] / ".gitignore"


def _compose_text() -> str:
    return (_DEPLOY_LOCAL / "docker-compose.yml").read_text(encoding="utf-8")


def test_compose_pins_the_stable_openemr_release_from_tick_001() -> None:
    compose = _compose_text()

    assert "image: openemr/openemr:8.3.0" in compose
    assert "openemr/openemr:latest" not in compose
    assert "openemr/openemr:flex" not in compose


def test_compose_pins_mariadb_rather_than_an_unversioned_tag() -> None:
    compose = _compose_text()

    assert "image: mariadb:" in compose
    assert "image: mariadb:latest" not in compose


def test_compose_runs_openemr_mariadb_and_ai_server_as_separate_services() -> None:
    compose = _compose_text()

    assert "\n  mariadb:\n" in compose
    assert "\n  openemr:\n" in compose
    assert "\n  ai-server:\n" in compose


def test_ai_session_volume_is_named_and_persists_across_restart() -> None:
    compose = _compose_text()

    assert "ai-session-data:/data" in compose
    assert "AI_SESSION_DATABASE_PATH: /data/ai_session.sqlite3" in compose
    # A named top-level volume (not `tmpfs` or a bind mount) survives
    # `docker compose restart` and `down` without `-v`.
    assert "\nvolumes:\n" in compose
    assert "  ai-session-data:\n" in compose


def test_mariadb_and_openemr_state_are_also_persistent_named_volumes() -> None:
    compose = _compose_text()

    assert "mariadb-data:/var/lib/mysql" in compose
    assert "openemr-sites:/var/www/localhost/htdocs/openemr/sites" in compose
    assert "  mariadb-data:\n" in compose
    assert "  openemr-sites:\n" in compose


def test_compose_declares_no_literal_secret_values() -> None:
    compose = _compose_text()

    for secret_var in (
        "MARIADB_ROOT_PASSWORD",
        "OPENEMR_MYSQL_PASSWORD",
        "OPENEMR_ADMIN_PASSWORD",
        "AI_SESSION_ENCRYPTION_KEY",
        "OPENEMR_OAUTH_CLIENT_SECRET",
        "GROQ_API_KEY",
    ):
        # Every secret is read from the environment (interpolated from the
        # untracked deploy/local/.env), never assigned a literal value in-file.
        assert f"${{{secret_var}" in compose


def test_env_example_has_no_real_secret_values() -> None:
    env_example = (_DEPLOY_LOCAL / ".env.example").read_text(encoding="utf-8")

    for secret_var in (
        "MARIADB_ROOT_PASSWORD",
        "OPENEMR_MYSQL_PASSWORD",
        "OPENEMR_ADMIN_PASSWORD",
        "AI_SESSION_ENCRYPTION_KEY",
        "OPENEMR_OAUTH_CLIENT_ID",
        "OPENEMR_OAUTH_CLIENT_SECRET",
        "GROQ_API_KEY",
    ):
        for line in env_example.splitlines():
            if line.startswith(f"{secret_var}="):
                assert line == f"{secret_var}=", f"{secret_var} must be a blank placeholder"


def test_deploy_local_env_is_excluded_from_source_control() -> None:
    gitignore = _GITIGNORE.read_text(encoding="utf-8")

    # These global (no leading slash) patterns apply at every directory depth,
    # including deploy/local/.env, while re-including deploy/local/.env.example.
    assert ".env" in gitignore.splitlines()
    assert "!.env.example" in gitignore.splitlines()


def test_default_llm_provider_requires_no_paid_service() -> None:
    env_example = (_DEPLOY_LOCAL / ".env.example").read_text(encoding="utf-8")

    assert "LLM_PROVIDER=groq" in env_example
    # Groq is used at its free tier; the key is optional until LLM endpoints
    # that use it are wired in, so the AI server must still be able to start.
    compose = _compose_text()
    assert "GROQ_API_KEY: ${GROQ_API_KEY:-}" in compose


def test_ai_server_dockerfile_installs_pinned_local_ocr_engine() -> None:
    dockerfile = (_DEPLOY_LOCAL / "ai-server.Dockerfile").read_text(encoding="utf-8")

    assert "tesseract-ocr" in dockerfile
    assert "tesseract-ocr-eng" in dockerfile
    assert "FROM ghcr.io/astral-sh/uv:0.11.33-python3.13-trixie-slim" in dockerfile


def test_ai_server_service_never_joins_the_mariadb_network() -> None:
    compose = _compose_text()
    services_block, top_level_networks = compose.split("\nnetworks:\n", 1)
    ai_server_section = services_block.split("\n  ai-server:\n", 1)[1]

    assert "- app" in ai_server_section
    assert "- emr" not in ai_server_section
    # The top-level network definitions still declare both networks.
    assert "emr:" in top_level_networks
    assert "app:" in top_level_networks
