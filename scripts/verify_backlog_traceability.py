"""Keep tickets/BACKLOG.md generated from ticket frontmatter and PRD requirement IDs."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


class BacklogError(Exception):
    """Raised when the backlog and the ticket directory have diverged."""


EPIC_TITLES = {
    "EPIC-01": "Resolve blocking contracts",
    "EPIC-02": "Establish reproducible foundation",
    "EPIC-03": "Secure runtime boundary",
    "EPIC-04": "Embed the patient chat",
    "EPIC-05": "Complete confirmed OCR",
    "EPIC-06": "Deliver guided onboarding",
    "EPIC-07": "Deliver authoritative scheduling",
    "EPIC-08": "Deploy and verify",
    "EPIC-09": "Replace hand-coded intent with a local model",
}

_REQUIREMENT_PATTERN = re.compile(r"^- \*\*((?:FR|NFR)-\d+)\b", re.MULTILINE)
_FRONTMATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_UNCOVERED_MARKER = "_no ticket_"


@dataclass(frozen=True)
class Ticket:
    """The BACKLOG-relevant frontmatter of a single ticket file.

    `status` is deliberately absent: it changes on every ticket transition, and
    rendering it here would make each status commit fail the generated-block check.
    """

    identifier: str
    title: str
    kind: str
    epic: str
    priority: str
    sources: tuple[str, ...]
    path: Path


def parse_requirement_ids(prd_path: Path) -> list[str]:
    """Return every requirement ID declared in the PRD, in FR-then-NFR numeric order."""
    if not prd_path.is_file():
        raise BacklogError(f"PRD is missing: {prd_path}")

    identifiers = set(_REQUIREMENT_PATTERN.findall(prd_path.read_text(encoding="utf-8")))
    if not identifiers:
        raise BacklogError(f"PRD declares no FR/NFR requirement IDs: {prd_path}")
    return sorted(identifiers, key=requirement_sort_key)


def requirement_sort_key(identifier: str) -> tuple[str, int]:
    """Order FR-2 before FR-10 and every FR before every NFR."""
    prefix, _, number = identifier.partition("-")
    return prefix, int(number)


def _parse_list(value: str) -> tuple[str, ...]:
    """Parse a `[A, B]` frontmatter list into a tuple of entries."""
    inner = value.strip().removeprefix("[").removesuffix("]")
    return tuple(entry.strip() for entry in inner.split(",") if entry.strip())


def parse_ticket(path: Path) -> Ticket:
    """Read one ticket file's frontmatter."""
    match = _FRONTMATTER_PATTERN.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise BacklogError(f"ticket has no frontmatter block: {path.name}")

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator and not key.startswith(" "):
            fields[key.strip()] = value.strip()

    missing = [key for key in ("id", "title", "type", "epic", "priority") if key not in fields]
    if missing:
        raise BacklogError(f"{path.name} frontmatter lacks: {', '.join(missing)}")

    return Ticket(
        identifier=fields["id"],
        title=fields["title"].strip('"'),
        kind=fields["type"],
        epic=fields["epic"],
        priority=fields["priority"],
        sources=_parse_list(fields.get("source", "[]")),
        path=path,
    )


def load_tickets(tickets_directory: Path) -> list[Ticket]:
    """Read every TICK-*.md file in ID order."""
    paths = sorted(tickets_directory.glob("TICK-*.md"))
    if not paths:
        raise BacklogError(f"no ticket files found in {tickets_directory}")
    return [parse_ticket(path) for path in paths]


def render_roster(tickets: list[Ticket]) -> str:
    """Render the per-epic ticket roster that keeps the narrative complete."""
    sections = []
    for epic in sorted({ticket.epic for ticket in tickets}):
        members = [ticket for ticket in tickets if ticket.epic == epic]
        title = EPIC_TITLES.get(epic, "Unnamed epic")
        rows = "\n".join(
            f"| {ticket.identifier} | {ticket.kind} | {ticket.priority} | {ticket.title} |"
            for ticket in members
        )
        sections.append(
            f"#### {epic} — {title} ({len(members)} tickets)\n\n"
            "| Ticket | Type | Priority | Title |\n"
            "|---|---|---|---|\n"
            f"{rows}"
        )
    return "\n\n".join(sections)


def render_traceability(tickets: list[Ticket], requirement_ids: list[str]) -> str:
    """Render the requirement table plus the tickets that trace to no requirement."""
    rows = []
    for requirement in requirement_ids:
        covering = [ticket.identifier for ticket in tickets if requirement in ticket.sources]
        rows.append(f"| {requirement} | {', '.join(covering) or _UNCOVERED_MARKER} |")

    untraced = [ticket.identifier for ticket in tickets if not ticket.sources]
    untraced_line = ", ".join(untraced) if untraced else "none"
    return (
        "| Requirement | Tickets |\n"
        "|---|---|\n" + "\n".join(rows) + "\n\n"
        f"Tickets with an empty `source:` (backlog hygiene, governed by no requirement): "
        f"{untraced_line}."
    )


def render_audit(tickets: list[Ticket], requirement_ids: list[str]) -> str:
    """Render the audit summary counts for the current tree."""
    covered = {source for ticket in tickets for source in ticket.sources}
    uncovered = [identifier for identifier in requirement_ids if identifier not in covered]
    uncovered_line = ", ".join(uncovered) if uncovered else "none"
    untraced = [ticket.identifier for ticket in tickets if not ticket.sources]
    return (
        f"- Tickets in `tickets/`: {len(tickets)} across "
        f"{len({ticket.epic for ticket in tickets})} epics; "
        f"tickets tracing to no requirement: {len(untraced)}.\n"
        f"- Requirements declared in `PRD.md`: {len(requirement_ids)}; "
        f"covered by at least one ticket: {len(covered)}.\n"
        f"- Requirements with no covering ticket: {uncovered_line}.\n"
        "- Every ticket ID above is present because "
        "`scripts/verify_backlog_traceability.py` fails CI otherwise; the audit is continuous, "
        "not a one-off review."
    )


def _block_pattern(name: str) -> re.Pattern[str]:
    marker = re.escape(name)
    return re.compile(
        rf"(<!-- generated:{marker}:begin -->\n).*?(\n<!-- generated:{marker}:end -->)",
        re.DOTALL,
    )


def replace_block(document: str, name: str, body: str) -> str:
    """Replace the contents of one generated block, leaving its markers in place."""
    pattern = _block_pattern(name)
    if pattern.search(document) is None:
        raise BacklogError(f"BACKLOG.md has no generated:{name} block")
    return pattern.sub(lambda match: f"{match.group(1)}{body}{match.group(2)}", document, count=1)


def render_backlog(document: str, tickets: list[Ticket], requirement_ids: list[str]) -> str:
    """Return the backlog document with every generated block refreshed."""
    document = replace_block(document, "roster", render_roster(tickets))
    document = replace_block(
        document, "traceability", render_traceability(tickets, requirement_ids)
    )
    return replace_block(document, "audit", render_audit(tickets, requirement_ids))


def check_backlog(document: str, tickets: list[Ticket], requirement_ids: list[str]) -> list[str]:
    """Return every reason the backlog no longer matches the ticket directory."""
    problems = []
    known = set(requirement_ids)

    for ticket in tickets:
        unknown = [source for source in ticket.sources if source not in known]
        if unknown:
            problems.append(
                f"{ticket.path.name}: source names requirement(s) absent from PRD.md: "
                f"{', '.join(unknown)}"
            )
        if ticket.epic not in EPIC_TITLES:
            problems.append(f"{ticket.path.name}: epic {ticket.epic} has no title in EPIC_TITLES")

    missing = [ticket.identifier for ticket in tickets if ticket.identifier not in document]
    if missing:
        problems.append(f"{len(missing)} ticket(s) missing from BACKLOG.md: {', '.join(missing)}")

    try:
        regenerated = render_backlog(document, tickets, requirement_ids)
    except BacklogError as error:
        problems.append(str(error))
    else:
        if document != regenerated:
            problems.append(
                "BACKLOG.md generated blocks are stale; "
                "run `python scripts/verify_backlog_traceability.py --write`"
            )
    return problems


def main() -> int:
    """Check, or with --write regenerate, the backlog's generated blocks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backlog", type=Path, default=Path("tickets/BACKLOG.md"))
    parser.add_argument("--tickets", type=Path, default=Path("tickets"))
    parser.add_argument("--prd", type=Path, default=Path("PRD.md"))
    parser.add_argument("--write", action="store_true", help="rewrite the generated blocks")
    arguments = parser.parse_args()

    try:
        requirement_ids = parse_requirement_ids(arguments.prd)
        tickets = load_tickets(arguments.tickets)
        document = arguments.backlog.read_text(encoding="utf-8")
        if arguments.write:
            arguments.backlog.write_text(
                render_backlog(document, tickets, requirement_ids), encoding="utf-8"
            )
            print(
                f"BACKLOG_REGENERATED: {len(tickets)} tickets, {len(requirement_ids)} requirements"
            )
            return 0
        problems = check_backlog(document, tickets, requirement_ids)
    except BacklogError as error:
        print(f"BACKLOG_TRACEABILITY_FAILED: {error}", file=sys.stderr)
        return 1

    if problems:
        for problem in problems:
            print(f"BACKLOG_TRACEABILITY_FAILED: {problem}", file=sys.stderr)
        return 1

    print("BACKLOG_TRACEABILITY_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
