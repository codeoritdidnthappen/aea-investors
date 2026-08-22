from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_backlog_traceability import (
    BacklogError,
    check_backlog,
    load_tickets,
    parse_requirement_ids,
    render_backlog,
    render_traceability,
)

REPOSITORY_ROOT = Path(__file__).parents[2]

BACKLOG_TEMPLATE = """# Backlog

<!-- generated:roster:begin -->

<!-- generated:roster:end -->

<!-- generated:traceability:begin -->

<!-- generated:traceability:end -->

<!-- generated:audit:begin -->

<!-- generated:audit:end -->
"""


def write_ticket(directory: Path, identifier: str, sources: str, epic: str = "EPIC-01") -> None:
    """Write a minimal ticket file with the frontmatter the checker reads."""
    (directory / f"{identifier}-example.md").write_text(
        "---\n"
        f"id: {identifier}\n"
        f'title: "task(scope): example {identifier}"\n'
        "type: task\n"
        f"epic: {epic}\n"
        "priority: P2\n"
        "estimate: S\n"
        "depends_on: []\n"
        "labels: [example]\n"
        f"source: {sources}\n"
        "status: todo\n"
        "---\n## Context\n\nExample.\n",
        encoding="utf-8",
    )


@pytest.fixture()
def requirements() -> list[str]:
    return ["FR-1", "FR-2", "NFR-33"]


def test_committed_backlog_covers_every_ticket_file() -> None:
    requirement_ids = parse_requirement_ids(REPOSITORY_ROOT / "PRD.md")
    tickets = load_tickets(REPOSITORY_ROOT / "tickets")
    document = (REPOSITORY_ROOT / "tickets" / "BACKLOG.md").read_text(encoding="utf-8")

    assert check_backlog(document, tickets, requirement_ids) == []


def test_prd_requirement_ids_are_parsed_without_prefix_collisions() -> None:
    requirement_ids = parse_requirement_ids(REPOSITORY_ROOT / "PRD.md")

    assert "FR-31" in requirement_ids
    assert "FR-32" in requirement_ids
    assert "NFR-33" in requirement_ids
    assert "FR-33" not in requirement_ids
    assert requirement_ids.index("FR-2") < requirement_ids.index("FR-10") < len(requirement_ids)


def test_missing_ticket_is_reported(tmp_path: Path, requirements: list[str]) -> None:
    write_ticket(tmp_path, "TICK-900", "[FR-1]")
    write_ticket(tmp_path, "TICK-901", "[FR-2]")
    tickets = load_tickets(tmp_path)
    document = render_backlog(BACKLOG_TEMPLATE, tickets[:1], requirements)

    problems = check_backlog(document, tickets, requirements)

    assert any("1 ticket(s) missing from BACKLOG.md: TICK-901" in problem for problem in problems)


def test_source_naming_an_absent_requirement_is_reported(
    tmp_path: Path, requirements: list[str]
) -> None:
    write_ticket(tmp_path, "TICK-900", "[FR-33]")
    tickets = load_tickets(tmp_path)
    document = render_backlog(BACKLOG_TEMPLATE, tickets, requirements)

    problems = check_backlog(document, tickets, requirements)

    assert any(
        "source names requirement(s) absent from PRD.md: FR-33" in problem for problem in problems
    )


def test_ticket_without_a_source_is_named_rather_than_dropped(
    tmp_path: Path, requirements: list[str]
) -> None:
    write_ticket(tmp_path, "TICK-900", "[]")
    tickets = load_tickets(tmp_path)

    table = render_traceability(tickets, requirements)

    assert "governed by no requirement): TICK-900." in table
    assert (
        check_backlog(
            render_backlog(BACKLOG_TEMPLATE, tickets, requirements), tickets, requirements
        )
        == []
    )


def test_requirement_with_no_ticket_is_visible_in_the_table(
    tmp_path: Path, requirements: list[str]
) -> None:
    write_ticket(tmp_path, "TICK-900", "[FR-1]")
    tickets = load_tickets(tmp_path)

    table = render_traceability(tickets, requirements)

    assert "| FR-2 | _no ticket_ |" in table
    assert "| FR-1 | TICK-900 |" in table


def test_stale_generated_block_is_reported_and_regeneration_is_idempotent(
    tmp_path: Path, requirements: list[str]
) -> None:
    write_ticket(tmp_path, "TICK-900", "[FR-1]")
    tickets = load_tickets(tmp_path)
    generated = render_backlog(BACKLOG_TEMPLATE, tickets, requirements)
    stale = generated.replace("| FR-1 | TICK-900 |", "| FR-1 | TICK-001 |")

    problems = check_backlog(stale, tickets, requirements)

    assert any("generated blocks are stale" in problem for problem in problems)
    assert render_backlog(generated, tickets, requirements) == generated


def test_backlog_without_generated_markers_is_reported(
    tmp_path: Path, requirements: list[str]
) -> None:
    write_ticket(tmp_path, "TICK-900", "[FR-1]")
    tickets = load_tickets(tmp_path)

    problems = check_backlog("# Backlog\n\nTICK-900\n", tickets, requirements)

    assert any("no generated:roster block" in problem for problem in problems)


def test_ticket_without_frontmatter_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "TICK-900-broken.md").write_text("no frontmatter here\n", encoding="utf-8")

    with pytest.raises(BacklogError, match="no frontmatter block"):
        load_tickets(tmp_path)
