"""TICK-057: the preflight must catch the faults that are silent at runtime.

Each test reproduces one way the local stack came up degraded without reporting
anything, and asserts the check refuses it. The last two assert the opposite --
that ordinary, correct configurations pass -- because a check that fires on
healthy input gets ignored, and an ignored check protects nothing.
"""

from __future__ import annotations

from pathlib import Path

from scripts.preflight_local_stack import (
    find_broken_mounts,
    find_missing_env_keys,
    find_worktree_start,
)

_COMPOSE = """services:
  openemr:
    volumes:
      - openemr-sites:/var/www/localhost/htdocs/openemr/sites
      - ../../openemr_modules/aeai-portal-chat:/var/www/html/custom_modules/aeai-portal-chat:ro
    environment:
      GUARDED: ${GUARDED:?set GUARDED}
      DEFAULTED: ${DEFAULTED:-https://example.test}
      BARE: ${BARE}
"""


def _stack(tmp_path: Path, compose: str = _COMPOSE) -> Path:
    deploy = tmp_path / "deploy" / "local"
    deploy.mkdir(parents=True)
    (deploy / "docker-compose.yml").write_text(compose, encoding="utf-8")
    return deploy / "docker-compose.yml"


def test_ticket_057_a_compose_file_inside_a_build_worktree_is_refused(tmp_path: Path) -> None:
    """The TICK-057 cause: mounts pinned to a path deleted when the build ends."""
    worktree = tmp_path / ".builder" / "worktrees" / "TICK-051"
    compose = _stack(worktree)
    problem = find_worktree_start(compose)
    assert problem is not None
    assert "build worktree" in problem


def test_ticket_057_the_main_checkout_is_not_mistaken_for_a_worktree(tmp_path: Path) -> None:
    assert find_worktree_start(_stack(tmp_path)) is None


def test_ticket_057_a_missing_mount_source_is_reported(tmp_path: Path) -> None:
    compose = _stack(tmp_path)
    problems = find_broken_mounts(compose)
    assert len(problems) == 1
    assert "does not exist" in problems[0]
    assert "aeai-portal-chat" in problems[0]


def test_ticket_057_an_empty_mount_source_directory_is_reported(tmp_path: Path) -> None:
    """The exact runtime symptom: the directory exists but the module is gone."""
    (tmp_path / "openemr_modules" / "aeai-portal-chat").mkdir(parents=True)
    problems = find_broken_mounts(_stack(tmp_path))
    assert len(problems) == 1
    assert "empty directory" in problems[0]


def test_ticket_057_a_populated_mount_source_passes(tmp_path: Path) -> None:
    module = tmp_path / "openemr_modules" / "aeai-portal-chat"
    module.mkdir(parents=True)
    (module / "openemr.bootstrap.php").write_text("<?php", encoding="utf-8")
    assert find_broken_mounts(_stack(tmp_path)) == []


def test_ticket_057_named_volumes_are_not_treated_as_bind_mounts(tmp_path: Path) -> None:
    """`openemr-sites:` has no source path on disk and must not be reported."""
    module = tmp_path / "openemr_modules" / "aeai-portal-chat"
    module.mkdir(parents=True)
    (module / "openemr.bootstrap.php").write_text("<?php", encoding="utf-8")
    assert all("openemr-sites" not in problem for problem in find_broken_mounts(_stack(tmp_path)))


def test_ticket_057_only_a_bare_compose_reference_is_reported_missing(tmp_path: Path) -> None:
    """A `${KEY:?}` stops Compose itself and `${KEY:-}` is optional; neither is noise here."""
    compose = _stack(tmp_path)
    deploy = compose.parent
    (deploy / ".env.example").write_text("GUARDED=a\nDEFAULTED=b\nBARE=c\n", encoding="utf-8")
    (deploy / ".env").write_text("GUARDED=a\n", encoding="utf-8")
    assert find_missing_env_keys(deploy / ".env", deploy / ".env.example", compose) == ["BARE"]


def test_ticket_057_a_complete_env_reports_nothing(tmp_path: Path) -> None:
    compose = _stack(tmp_path)
    deploy = compose.parent
    (deploy / ".env.example").write_text("GUARDED=a\nDEFAULTED=b\nBARE=c\n", encoding="utf-8")
    (deploy / ".env").write_text("GUARDED=a\nDEFAULTED=b\nBARE=c\n", encoding="utf-8")
    assert find_missing_env_keys(deploy / ".env", deploy / ".env.example", compose) == []
