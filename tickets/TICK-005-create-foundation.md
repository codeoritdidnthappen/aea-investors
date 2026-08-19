---
id: TICK-005
title: "chore(foundation): create pinned project and CI baseline"
type: chore
epic: EPIC-02
priority: P1
estimate: M
depends_on: []
labels: [ci, foundation]
source: [NFR-18, NFR-21]
status: done
builder_commit: 25e8f96
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/6
---

## Context

The project needs a reproducible Python/FastAPI/LangGraph foundation for local
synthetic-data work, with coverage and AI-use records enforced from the start. Endpoint
discovery gates only the adapter operations that require it.

## Acceptance Criteria

- [ ] The approved repository layout, pinned runtime dependencies, and local developer commands are documented.
- [ ] CI runs formatting, static checks, unit tests, and coverage reporting with an 80% core-logic threshold.
- [ ] AI_USAGE.md records the required runtime model, OCR engine, prompt versions, and material AI-assisted development.

## Testing

Run the complete CI command from a clean checkout and demonstrate the threshold gate. CI must be green.

## Out of Scope

Business features or deployment infrastructure.
