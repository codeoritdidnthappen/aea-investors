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
    """NFR-15 still holds now that OpenEMR is built rather than run directly.

    TICK-057 moved the pin from `image:` in the compose file to `FROM` in
    `openemr.Dockerfile`, so the override templates could be copied in instead of
    bind-mounted one file at a time. The release must still be pinned exactly, and
    an unversioned tag must not appear in either file.
    """
    compose = _compose_text()
    dockerfile = (
        Path(__file__).resolve().parents[2] / "deploy/local/openemr.Dockerfile"
    ).read_text(encoding="utf-8")

    assert "dockerfile: deploy/local/openemr.Dockerfile" in compose
    assert "FROM openemr/openemr:8.3.0" in dockerfile
    for unversioned in ("openemr/openemr:latest", "openemr/openemr:flex"):
        assert unversioned not in compose
        assert unversioned not in dockerfile


def test_ticket_057_openemr_overrides_are_copied_in_not_bind_mounted() -> None:
    """A single-file bind mount whose source vanishes shadows the vendor original.

    It leaves an empty file at the destination rather than falling back, which
    blanked both OAuth pages in TICK-057 with nothing logged.
    """
    compose = _compose_text()
    dockerfile = (
        Path(__file__).resolve().parents[2] / "deploy/local/openemr.Dockerfile"
    ).read_text(encoding="utf-8")

    for template in ("oauth2-login.html.twig", "scope-authorize.html.twig"):
        assert f"COPY openemr_overrides/templates/oauth2/{template}" in dockerfile
        assert f"{template}:/var/www" not in compose


def test_ticket_057_openemr_healthcheck_asserts_the_module_is_present() -> None:
    """A mount can go empty underneath a running container; readiness alone misses it."""
    compose = _compose_text()

    assert "CMD-SHELL" in compose
    assert "test -s /var/www/localhost/htdocs/openemr/interface/modules" in compose
    assert "PortalChatController.php" in compose


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


# --- TICK-051: the destination and the chat origin ship as two wired settings -------


def test_the_split_settings_are_both_wired_with_a_required_variable_guard() -> None:
    """TICK-051 AC8: a new setting without a matching compose entry either fails the
    boot check or silently leaves the container on the old single value, so compose and
    `.env.example` are checked together here.

    Both carry a `:?` required-variable guard rather than a `:-` default (contrast
    `AI_SESSION_PORTAL_ORIGIN`, which is genuinely optional): a deployment that has not
    repointed its `.env` must stop at `docker compose up`, not start with the patient
    still landing on the full-page chat.
    """
    compose = _compose_text()
    env_example = (_DEPLOY_LOCAL / ".env.example").read_text(encoding="utf-8")

    assert "AI_SESSION_DASHBOARD_REDIRECT_URI: ${AI_SESSION_DASHBOARD_REDIRECT_URI:?" in compose
    assert "AI_SESSION_CHAT_ORIGIN: ${AI_SESSION_CHAT_ORIGIN:?" in compose
    assert "AI_SESSION_DASHBOARD_REDIRECT_URI=https://emr.localhost/portal/home.php" in env_example
    assert "AI_SESSION_CHAT_ORIGIN=https://chat.localhost" in env_example


def test_the_replaced_single_redirect_setting_is_no_longer_wired_anywhere() -> None:
    """TICK-051 AC10: renamed, not reused.

    A surviving `AI_SESSION_SUCCESS_REDIRECT_URI` *entry* is the exact failure the
    rename exists to prevent -- the AI server no longer reads that name, so an entry
    still carrying it looks configured while doing nothing at all.

    Checked as assignments and compose keys rather than as bare text, because both
    files mention the old name deliberately, in the comments explaining what replaced
    it. Removing that explanation is not the goal; removing the wiring is.
    """
    compose = _compose_text()
    env_example = (_DEPLOY_LOCAL / ".env.example").read_text(encoding="utf-8")

    for line in compose.splitlines():
        assert not line.strip().startswith("AI_SESSION_SUCCESS_REDIRECT_URI:")
    assert "${AI_SESSION_SUCCESS_REDIRECT_URI" not in compose
    for line in env_example.splitlines():
        assert not line.startswith("AI_SESSION_SUCCESS_REDIRECT_URI=")


def test_the_two_settings_are_documented_as_not_interchangeable() -> None:
    """TICK-051 AC8: each file carries a comment saying so.

    They look near-identical in a `.env`, and collapsing them back into one is the
    single change that reintroduces both bugs at once (ADR-8 forbids it). The comment
    is what a person editing these files actually reads.
    """
    compose = _compose_text()
    env_example = (_DEPLOY_LOCAL / ".env.example").read_text(encoding="utf-8")

    for text in (compose, env_example):
        # Matched on the word alone: both comments wrap across lines, and pinning the
        # exact surrounding phrasing would make this a formatting test.
        assert "interchangeable" in text


# --- TICK-059: the local model server runs in the topology, pinned ----------------


def test_the_model_server_runs_as_a_service_in_the_local_topology() -> None:
    """AC1. LOCAL_LLM_SPEC D7 puts Ollama in development and vLLM in deployment."""
    compose = _compose_text()

    assert "\n  ollama:\n" in compose
    assert "dockerfile: deploy/local/ollama.Dockerfile" in compose


def test_the_ai_server_reaches_the_model_server_by_service_name() -> None:
    """AC1. A host address only works on the one machine running Ollama natively.

    `ai_server/llm/local.py` falls back to `http://localhost:11434`, which inside the
    ai-server container is the AI server itself. Wiring the Docker service name is
    what makes the two containers actually find each other.
    """
    compose = _compose_text()

    assert "OLLAMA_HOST: ${OLLAMA_HOST:-http://ollama:11434}" in compose
    assert "OLLAMA_HOST: ${OLLAMA_HOST:-http://localhost" not in compose


def test_the_model_is_pinned_by_name_and_by_digest() -> None:
    """AC2. The name is a mutable pointer; only the digest fixes the bytes.

    Both defaults live in this committed file rather than in the gitignored `.env`,
    because a pin only the machine that happens to be running can see is not a pin
    (NFR-15, same reasoning as the mariadb/openemr/caddy tags).
    """
    compose = _compose_text()

    assert "LLM_MODEL: ${LLM_MODEL:-qwen2.5:7b-instruct-q4_K_M}" in compose
    assert (
        "LLM_MODEL_DIGEST: ${LLM_MODEL_DIGEST:-sha256:"
        "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e}"
    ) in compose
    assert "LLM_MODEL: ${LLM_MODEL:-qwen2.5:latest}" not in compose


def test_the_model_server_and_the_ai_server_name_the_same_model() -> None:
    """AC2. The name is wired twice; drift is a 404 on every turn, not a startup error.

    The model server pins one model and the AI server asks for another, both report
    healthy, and only a chat turn reveals it.
    """
    compose = _compose_text()
    wired = {
        line.strip() for line in compose.splitlines() if line.strip().startswith("LLM_MODEL: ")
    }

    assert len(wired) == 1, f"the model is wired inconsistently: {sorted(wired)}"


def test_the_model_image_is_pinned_and_never_latest_or_a_release_candidate() -> None:
    """NFR-15, applied to the server as well as the model it serves.

    Comment lines are dropped before matching: they name the tags being avoided in
    order to explain why, and matching against prose would fail on the explanation.
    """
    dockerfile = (_DEPLOY_LOCAL / "ollama.Dockerfile").read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in dockerfile.splitlines() if not line.strip().startswith("#")
    )

    assert "FROM ollama/ollama:0.32.15" in directives
    for unpinned in ("ollama/ollama:latest", "ollama/ollama:rocm"):
        assert unpinned not in directives
    # A release candidate is not a release; it can be replaced or withdrawn.
    assert "-rc" not in directives


def test_the_pinned_model_is_pulled_without_a_manual_step() -> None:
    """AC3. A stack that starts and then cannot answer is the failure mode twice had.

    The entrypoint pulls on a cold volume, so a clean checkout needs no README step
    to forget, and verifies the digest on *every* start, so a pull that never
    happened -- or happened against a moved tag -- is loud rather than silent.
    """
    entrypoint = (_DEPLOY_LOCAL / "ollama-entrypoint.sh").read_text(encoding="utf-8")

    assert 'ollama pull "$LLM_MODEL"' in entrypoint
    assert 'sha256sum "$MANIFEST"' in entrypoint
    # Refuses to serve on a mismatch: wrong-but-answering is worse than unavailable.
    assert 'if [ "$ACTUAL" != "$EXPECTED" ]; then' in entrypoint
    assert "exit 1" in entrypoint


def test_the_model_healthcheck_asserts_the_model_and_not_just_the_process() -> None:
    """AC6, TICK-057's lesson: a listening process with no model still fails every turn."""
    compose = _compose_text()
    healthcheck = (_DEPLOY_LOCAL / "ollama-healthcheck.sh").read_text(encoding="utf-8")

    assert 'test: ["CMD", "/usr/local/bin/aeai-ollama-healthcheck"]' in compose
    assert 'sha256sum "$MANIFEST"' in healthcheck
    assert "ollama list" in healthcheck


def test_the_model_volume_is_named_so_a_recreate_does_not_re_download_it() -> None:
    """AC4. Several GB; a recreate must reuse them."""
    compose = _compose_text()

    assert "ollama-models:/root/.ollama" in compose
    assert "  ollama-models:\n" in compose


def _ollama_directives() -> str:
    """The ollama service's actual YAML, with comment lines dropped.

    The comments deliberately discuss GPUs and ports -- they explain why neither is
    configured -- so matching against them would turn every explanation into a test
    failure.
    """
    compose = _compose_text()
    section = compose.split("\n  ollama:\n", 1)[1].split("\n  ai-server:\n", 1)[0]
    return "\n".join(line for line in section.splitlines() if not line.strip().startswith("#"))


def test_the_model_server_starts_on_a_machine_with_no_gpu() -> None:
    """AC5. Slow is acceptable; failing to start is not.

    A `deploy.resources.reservations.devices` GPU reservation hard-fails Compose on
    any host without an accelerator, so its absence is the requirement. Ollama falls
    back to CPU on its own.
    """
    directives = _ollama_directives()

    assert "devices:" not in directives
    assert "capabilities:" not in directives
    assert "gpu" not in directives.lower()


def test_the_model_server_publishes_no_host_port() -> None:
    """Reachable as `ollama` on the `app` network only, like the AI server itself."""
    directives = _ollama_directives()

    assert "ports:" not in directives
    assert "- app" in directives
    assert "- emr" not in directives


def test_verify_stack_covers_the_model_server_the_way_it_covers_the_others() -> None:
    """AC6: a missing model or an unreachable server is reported before a patient finds it."""
    verify = (_DEPLOY_LOCAL / "verify-stack.sh").read_text(encoding="utf-8")

    assert "aeai-ollama-healthcheck" in verify
    # The reachability leg has to run *inside* the ai-server container: it is the
    # only place the configured service name is actually resolved.
    assert "docker compose exec -T ai-server" in verify
    assert "OLLAMA_HOST" in verify


def test_verify_stack_tests_for_a_running_container_not_for_an_exit_status() -> None:
    """`docker compose ps --status running <svc>` exits 0 even when nothing matched.

    Confirmed live (evidence/TICK-059): with the model server stopped, the command
    still succeeded, so a check written as `if ! docker compose ps ...` never fires
    -- a guard that cannot fail, in the file whose whole purpose is catching silent
    failure. `--quiet` plus a test for empty *output* is what actually discriminates.
    """
    verify = (_DEPLOY_LOCAL / "verify-stack.sh").read_text(encoding="utf-8")

    for service in ("ollama", "ai-server"):
        assert f"docker compose ps --status running --quiet {service}" in verify
        assert f"! docker compose ps --status running {service}" not in verify


def test_ai_usage_records_the_pinned_local_model_and_its_quantisation() -> None:
    """AC7, NFR-21: alongside the existing external-model and OCR entries."""
    ai_usage = (Path(__file__).resolve().parents[2] / "AI_USAGE.md").read_text(encoding="utf-8")

    assert "Local language model" in ai_usage
    assert "qwen2.5:7b-instruct-q4_K_M" in ai_usage
    assert "Q4_K_M" in ai_usage
    assert "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e" in ai_usage


def test_the_dashboard_destination_and_the_chat_origin_are_different_hosts() -> None:
    """TICK-051 AC2: the shipped values must actually differ, or the split is inert.

    A deployment where both point at the chat host passes every structural check above
    and still strands the patient on the full-page chat.
    """
    env_example = (_DEPLOY_LOCAL / ".env.example").read_text(encoding="utf-8")

    assert "AI_SESSION_DASHBOARD_REDIRECT_URI=https://emr.localhost" in env_example
    assert "AI_SESSION_CHAT_ORIGIN=https://chat.localhost" in env_example
