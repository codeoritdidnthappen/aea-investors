---
id: TICK-053
title: "chore(tickets): BACKLOG.md traceability stops at TICK-031 and omits every later ticket"
type: task
epic: EPIC-02
priority: P2
estimate: M
depends_on: []
labels: [documentation]
source: []
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/106
builder_commit: b1d51e9
---
## Context

`tickets/BACKLOG.md` calls itself the reviewable source of truth and carries
the requirement-traceability table mapping every FR/NFR to the tickets that
satisfy it. It was written once during the original generation run and has
not been updated since.

The highest ID the backlog mentions anywhere is TICK-031. Every ticket filed
since is absent entirely -- as of 2026-08-22 that is 27 of 56 files:
TICK-029, TICK-030, and TICK-032 through TICK-056.

The count is deliberately not in this ticket's title, and the fix should not
hard-code it either: it has already gone stale twice while this ticket sat in
the backlog, which is the point.

The practical effect is that the traceability table understates coverage.
`FR-2`, for example, lists only TICK-002, TICK-012, TICK-024, while
TICK-032, TICK-045, TICK-046, TICK-047, TICK-051, and TICK-054 all cite
it.
Anyone using the table to answer "what covers this requirement" or "is
anything untraced" gets an answer that is wrong by 27 tickets, and the
closing "Audit summary" section still describes the audit as pending from
the original run.

`source: []` on this ticket is deliberate: this is backlog hygiene and no
PRD requirement governs it. That is itself a small argument for the fix --
there is currently no automated signal when the backlog and the ticket
directory diverge.

## Acceptance Criteria

- [ ] Every `tickets/TICK-*.md` file is represented in `BACKLOG.md`: in the
      epic/execution-order narrative, and in the traceability table under
      each `source:` ID its frontmatter declares.
- [ ] The traceability table is regenerated from ticket frontmatter rather
      than hand-edited, so it is reproducible and the next 20 tickets do
      not recreate this gap.
- [ ] Any FR/NFR with no ticket covering it is visible as such rather than
      silently absent from the table.
- [ ] Tickets whose `source:` is empty or names a non-existent requirement
      are reported, not silently dropped. This ticket cites nothing, and the
      check must also catch the inverse error: an `FR-3x` id that matches
      only because `NFR-3x` exists.
- [ ] The "Audit summary" section reflects the current state instead of
      describing the original run's audit as pending.
- [ ] The epic narrative covers EPIC-04's later bug tickets, which is where
      most of the 22 landed.

## Testing

A committed check that fails when a ticket file is missing from
`BACKLOG.md`, or when a ticket's `source:` names an ID absent from `PRD.md`
-- run it against the current tree and confirm it goes from failing (27
missing at time of writing) to passing. Wire it into CI next to
`scripts/verify_release_governance.py` so the two artefacts cannot drift
apart again silently. CI must be green.

## Out of Scope

Changing any ticket's content, status, priority, or `source:` values --
except where a `source:` names a requirement that does not exist, which is
reported here and fixed in its own ticket. Re-running the ticket-agent
generator over the backlog; this is a repair of the existing file, not a
regeneration.
