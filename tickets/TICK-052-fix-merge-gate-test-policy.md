---
id: TICK-052
title: "bug(process): merge gate tells agents there is no test suite and not to write tests"
type: task
epic: EPIC-02
priority: P1
estimate: S
depends_on: []
labels: [documentation, bug]
source: [NFR-18]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/105
builder_commit: 8d70a9e
---
## Context

`GIT_WORKFLOW_COIDH.md` is the authoritative workflow doc -- `CLAUDE.md`
section 5 points every agent at it before any git work. Its mandatory
merge gate currently opens with:

> **No test suite — by decision, for now.** This project has no tests and
> none are required. Do not write tests to satisfy this gate, and do not
> treat their absence as a gate that passed on its own.

Every clause of that is false as of 2026-08-22:

- `ai_server/tests/` contains **34** `test_*.py` files.
- `.github/workflows/ci.yml` runs `uv run --locked --group dev pytest` on
  every push and pull request, alongside `ruff` and
  `scripts/verify_release_governance.py`.
- `.gitlab-ci.yml` runs the same `pytest` invocation.
- A `.coverage` database is present at the repo root.
- **NFR-18 [must]** in `PRD.md:200` requires "at least 80% automated test
  coverage" of core logic. The doc instructs agents to violate a `must`
  requirement.

This is not cosmetic drift. The doc is read by autonomous agents as
binding instruction, and it tells them (a) not to write tests, and (b) that
the gate has no test component -- while CI simultaneously fails their merge
if `pytest` is red. It also contradicts the Testing section of essentially
every ticket in `tickets/`, all of which end "CI must be green."

## Acceptance Criteria

- [ ] The merge gate's testing clause states the actual policy: the suite
      exists, `pytest` runs in CI on push and PR, and it must be green
      before merge.
- [ ] The instruction not to write tests is removed. Whatever replaces it
      is consistent with NFR-18's 80% coverage requirement and with the
      per-ticket "CI must be green" convention.
- [ ] The gate names what an agent actually has to run locally before
      opening a PR, matching the CI job's commands (`ruff format --check`,
      `ruff check`, `pytest`, and the release-governance script), so a
      green local run and a green CI run mean the same thing.
- [ ] The "Verification first" section is reconciled: it currently reasons
      from "with no test suite, this rule carries the whole weight." Live
      verification stays required -- several tickets depend on it -- but
      the stated reason is no longer true.
- [ ] No other claim in `GIT_WORKFLOW_COIDH.md` is left contradicting the
      repo. `[PROJECT_NAME]` placeholders in the `gh` examples are either
      filled in with `aea-investors` or explicitly marked as placeholders.

## Testing

Documentation change; verification is by reading the doc back against the
two CI configs and `ai_server/tests/`, and confirming no remaining sentence
asserts the absence of tests. Confirm `uv run --locked --group dev pytest`
is green locally and record the summary line in the PR body. CI must be
green.

## Out of Scope

Changing the CI configuration, the test suite, or the coverage threshold.
Raising or enforcing NFR-18's 80% figure -- this ticket only stops the
workflow doc from contradicting it. The `/code-review` requirement in the
gate is correct and stays.
