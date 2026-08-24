---
id: TICK-064
title: "feat(privacy): narrow Groq to restated general knowledge and stop forwarding patient text"
type: feature
epic: EPIC-09
priority: P1
estimate: M
depends_on: [TICK-063]
labels: [llm, privacy, backend]
source: [FR-34]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/129
builder_commit: f2607d4
---
## Context

`docs/LOCAL_LLM_SPEC.md` D3, D4, D13, D14.

Today `ai_server/app/chat.py:427` does this:

```python
OutboundMessage(role="user", content=message),
```

The patient's raw typed text is sent to Groq, and Presidio scanning that text is the
*only* control. That is a detector, and detectors fail open on what they do not
recognise -- which is the same unbounded-cases problem as the parsers, relocated.

After this ticket the boundary is structural. Groq receives only content this system
generated: for a general-knowledge question, the local model emits a canonical,
context-free **restatement** as a schema'd field, and that restatement is what
leaves. The patient's words never egress. Presidio still screens the constructed
payload -- two independent controls where there was one, and the primary one is no
longer a prediction about text.

Scheduling planning moves to the local model (D13), so `PlanningOutput`,
`SchedulingContext` and `SchedulingRules` stop being an outbound concern.

## Acceptance Criteria

- [ ] No code path can place patient-typed text in an outbound request. Enforced by
      the types, so it is a compile-or-construct-time property rather than a rule
      someone must remember: an outbound payload cannot be built from a raw turn.
- [ ] `ask_general_knowledge` carries only the model's restatement plus content this
      codebase authored.
- [ ] Presidio screens every constructed payload before egress and still rejects
      rather than scrubs, preserving ADR-5. A rejection is reported honestly rather
      than answered from nothing.
- [ ] The patient can see what was asked on their behalf, or the restatement is
      otherwise inspectable. They never see it today, so a distorted restatement
      would silently answer a question they did not ask.
- [ ] Outbound payload types no longer carry scheduling context, since scheduling is
      local now. Dead types are removed rather than left to look load-bearing.
- [ ] Groq being unavailable degrades only general-knowledge answers. Everything
      patient-specific is local and must keep working.

## Testing

Unit tests asserting an outbound payload cannot be constructed from a raw patient
turn, that only a restatement reaches the wire, and that Presidio still rejects a
seeded PHI-bearing restatement. A test with seeded sensitive values proving the
privacy boundary, matching the bar NFR-28 already sets for the gate. CI must be
green.

## Out of Scope

Removing Groq entirely -- D13 keeps it for general knowledge. The local model's
restatement prompt quality, which TICK-062's corpus measures.
