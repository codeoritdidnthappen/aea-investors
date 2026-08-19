---
id: TICK-007
title: "chore(governance): enforce evaluation-data and AI-use release gates"
type: chore
epic: EPIC-02
priority: P1
estimate: S
depends_on: [TICK-005, TICK-006]
labels: [ci, privacy, governance]
source: [NFR-21, NFR-24]
status: todo
remote_url: null
---

## Context

AI-use records and evaluator-only data must be auditable without shipping sensitive synthetic source material in application artifacts.

## Acceptance Criteria

- [ ] CI fails when AI_USAGE.md lacks required pinned-runtime or prompt-version entries.
- [ ] CI fails when deployment artifacts contain OCR labels, fixture-source records, or privacy golden-corpus data.
- [ ] Release documentation explains the gate outputs without exposing fixture values.

## Testing

Add passing and deliberately failing packaging fixtures; run CI. CI must be green.

## Out of Scope

Runtime privacy-gate implementation.
