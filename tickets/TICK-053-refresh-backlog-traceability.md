---
id: TICK-053
title: "chore(tickets): BACKLOG.md traceability stops at TICK-031 and omits 22 tickets"
type: task
epic: EPIC-02
priority: P2
estimate: M
depends_on: []
labels: [documentation]
source: []
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/106
builder_commit: null
---
## Context

`tickets/BACKLOG.md` calls itself the reviewable source of truth and carries
the requirement-traceability table mapping every FR/NFR to the tickets that
satisfy it. It was written once during the original generation run and has
not been updated since.

51 ticket files exist. The highest ID the backlog mentions anywhere is
TICK-031. These 22 are absent entirely:

```
TICK-029, TICK-030, TICK-032, TICK-033, TICK-034, TICK-035, TICK-036,
TICK-037, TICK-038, TICK-039, TICK-040, TICK-041, TICK-042, TICK-043,
TICK-044, TICK-045, TICK-046, TICK-047, TICK-048, TICK-049, TICK-050,
TICK-051
```

The practical effect is that the traceability table understates coverage.
`FR-2`, for example, lists only TICK-002, TICK-012, TICK-024, while
TICK-032, TICK-045, TICK-046, TICK-047, and TICK-051 all landed against it.
Anyone using the table to answer "what covers this requirement" or "is
anything untraced" gets an answer that is wrong by 22 tickets, and the
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
      are reported, not silently dropped. TICK-051 currently cites NFR-19
      (accessibility), which does not govern it, and this ticket cites
      nothing.
- [ ] The "Audit summary" section reflects the current state instead of
      describing the original run's audit as pending.
- [ ] The epic narrative covers EPIC-04's later bug tickets, which is where
      most of the 22 landed.

## Testing

A committed check that fails when a ticket file is missing from
`BACKLOG.md`, or when a ticket's `source:` names an ID absent from `PRD.md`
-- run it against the current tree and confirm it goes from failing (22
missing) to passing. Wire it into CI next to
`scripts/verify_release_governance.py` so the two artefacts cannot drift
apart again silently. CI must be green.

## Out of Scope

Changing any ticket's content, status, priority, or `source:` values --
except where a `source:` names a requirement that does not exist, which is
reported here and fixed in its own ticket. Re-running the ticket-agent
generator over the backlog; this is a repair of the existing file, not a
regeneration.
