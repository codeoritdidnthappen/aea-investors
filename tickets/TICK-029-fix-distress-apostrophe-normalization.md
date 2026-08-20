---
id: TICK-029
title: "task(onboarding): normalize curly apostrophes in distress-phrase matching"
type: task
epic: EPIC-06
priority: P1
estimate: XS
depends_on: [TICK-017]
labels: [onboarding, safety]
source: [FR-8, NFR-3]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/54
builder_commit: 214b589
---

## Context

`ai_server/onboarding/triggers.py`'s `_normalize()` only lowercases and collapses
whitespace before matching `ONBOARDING_CONTRACT.md`'s approved distress phrase
corpus. It does not normalize curly/smart apostrophes (`’` U+2019) to straight ones
(`'` U+0027). A patient typing "I can't keep myself safe" with a curly apostrophe —
the default on iOS and most word processors' autocorrect — silently fails to match
`IMMEDIATE_SAFETY_PHRASES`, and the 988/911 supportive content never shows for a
message that should trigger it. This affects every apostrophe-containing phrase in
both the general-distress and immediate-safety lists. Found by `/code-review`
during TICK-017 (PR #53); not fixed there per explicit direction to land the
feature first and track this separately given its safety relevance.

## Acceptance Criteria

- [ ] `_normalize()` normalizes `’` (U+2019) and other common smart-quote variants
      to `'` (U+0027), alongside the existing lowercase/whitespace collapsing.
- [ ] A curly-apostrophe variant of at least one immediate-safety phrase and one
      general-distress phrase is added to the test corpus and confirmed to trigger
      the correct supportive content.
- [ ] Every existing distress-trigger test still passes.

## Testing

Extend `ai_server/tests/test_onboarding_triggers.py` with curly-apostrophe
fixtures. CI must be green.

## Out of Scope

Any other punctuation/unicode normalization beyond apostrophes; expanding the
approved phrase corpus itself.
