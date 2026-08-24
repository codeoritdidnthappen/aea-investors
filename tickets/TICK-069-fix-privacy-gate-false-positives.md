---
id: TICK-069
title: "bug(privacy): the gate treats any Presidio entity as PHI, so ordinary questions are refused and the patient is told they sent personal information"
type: task
epic: EPIC-09
priority: P1
estimate: S
depends_on: [TICK-064]
labels: [privacy, llm, backend, bug]
source: [FR-34, D3, D4]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/136
builder_commit: null
---
## Context

Found by live verification of EPIC-09 against a rebuilt local stack, 2026-08-23.

`PrivacyGate.has_sensitive_text` treats any Presidio result, of any entity type, at
any confidence, as sensitive:

    ai_server/privacy/gate.py:230-232
    def has_sensitive_text(self, text: str) -> bool:
        return bool(self._analyzer.analyze(text=text, language="en"))

`AnalyzerEngine.analyze` with no `entities` argument runs every built-in recognizer,
and with no `score_threshold` returns everything above zero. `LOCATION` and `DATE_TIME`
are built-in recognizers. Neither is PHI on its own, and both appear in ordinary
questions.

Measured directly against the deployed gate:

    'what is the capital of France'              -> LOCATION  0.85 'France'          BLOCKED
    "What are the clinic's opening hours?"       -> DATE_TIME 0.85 'opening hours'   BLOCKED
    'What brings you to our mental health clinic today?' -> DATE_TIME 0.85 'today'   BLOCKED
    'What is cognitive behavioral therapy?'      -> (none)                           allowed
    'How do you get to the clinic by bus?'       -> (none)                           allowed

The patient receives: "I did not send it, because it looked like it contained personal
or health information ... Please ask again without any personal details in it." For
"what is the capital of France?" that statement is simply false, and there is no
rephrasing that helps, because the patient did not write the flagged text -- the model's
restatement did.

This is not a regression from EPIC-09: the code is identical at `a291ad1`. What changed
is its reach. Before TICK-063/064 the deterministic handlers answered most turns and the
gate screened raw patient text, where over-blocking was the safe direction. Since
TICK-064 the gate screens only the model's own minted restatement, and answering those
restatements is the entire remaining job of the external provider (D13). The gate now
rejects precisely the traffic TICK-064 was designed to permit.

The direction of the fix is a decision, not an implementation detail: ADR-5 rejects
rather than scrubs, and that stays. The question is which entity types justify a
rejection. `MEDICAL_RECORD_NUMBER`, `OPENEMR_IDENTIFIER`, `HEALTHCARE_IDENTIFIER`,
`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_SSN` and similar clearly do.
`DATE_TIME` and `LOCATION` clearly do not, on a context-free restatement that by
construction carries no patient record.

## Acceptance Criteria

1. The screened entity set is explicit in code, with a stated reason per entity for why
   it does or does not justify rejection. No reliance on Presidio's default of "every
   recognizer".
2. All five probes above return the correct verdict: the three currently-blocked
   questions pass, and the two that pass still pass.
3. Text carrying real identifiers is still rejected and never sent in any form:
   `MRN-12345`, `OE-ABC123`, `NPI-1234567890`, a full name, an email address and a
   phone number each still block, asserted individually.
4. A restatement that carries a patient detail over from the turn it restates is still
   caught -- the case the gate's own docstring names as its reason for existing.
5. Live verification against a running stack: "what is the capital of France?" and
   "what are your opening hours?" both answered, transcript recorded, in the shape of
   `evidence/TICK-065`.

## Out of Scope

Whether the model should be calling `ask_general_knowledge` for "what are your opening
hours?" at all -- clinic hours are clinic data, not general knowledge, and arguably want
their own tool. Noted here, not fixed here.
