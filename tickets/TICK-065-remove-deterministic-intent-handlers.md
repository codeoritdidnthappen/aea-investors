---
id: TICK-065
title: "refactor(chat): delete the deterministic intent handlers and report honestly when the model is down"
type: task
epic: EPIC-09
priority: P1
estimate: M
depends_on: [TICK-063, TICK-064]
labels: [llm, chat, backend]
source: [FR-33]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/130
builder_commit: 1b50d49
---
## Context

`docs/LOCAL_LLM_SPEC.md` D12. Once the model owns every turn,
`ai_server/app/address_chat.py` (555 lines) and
`ai_server/app/onboarding_chat.py` (510 lines) are dead code. They are also the
largest concentration of exactly the approach this epic exists to end: intent
detection by pattern and field extraction by text parsing.

Keeping them as an outage fallback was considered and rejected. A path nobody
exercises rots, and when it finally runs it produces the failure this project
already had -- a bad parse reaching a chart -- at the worst possible moment, with
nobody watching. An honest "unavailable" is better than silently worse behaviour.

The consequence is accepted and must be handled well: model-server availability
becomes chat availability.

## Acceptance Criteria

- [ ] Both modules are deleted, along with the mode-detection functions that routed
      to them and any now-unused helpers they owned. Their tests go too; tests for
      behaviour that still exists move to the new path rather than being deleted.
- [ ] No pattern-matching intent detection remains anywhere in the turn path. If any
      survives, it is named with a reason, not left silently.
- [ ] When the model server is unreachable the chat says so plainly -- that the
      assistant is temporarily unavailable and the portal still works -- without
      naming internal components, and without offering a degraded path that might
      write.
- [ ] `/health` reports model-server reachability alongside its existing
      dependencies, so an outage is visible in monitoring before a patient finds it.
- [ ] No write path can execute while the model is unavailable. Failing closed on
      writes is the point.
- [ ] Documentation catches up: `ARCHITECTURE.md`'s components and request flows, and
      `README.md`'s description of how a turn is handled, describe what now happens.

## Testing

Assert the deleted symbols are gone and nothing imports them. Tests for the
unavailable path: chat reports unavailable, `/health` shows the dependency down, and
a write is refused rather than attempted. Live verification with the model server
stopped, confirming the patient sees an honest message and the portal is unaffected,
recorded under `evidence/TICK-065/`. CI must be green.

## Out of Scope

The OCR pipeline, the privacy gate, and the scheduling services -- all still used,
now reached through tools instead of hand-written routing.
