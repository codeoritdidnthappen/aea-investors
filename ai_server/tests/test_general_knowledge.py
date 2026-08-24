"""TICK-064: the outbound boundary is structural, not a rule someone must remember.

These tests are about what is *impossible*, not what happens to be true today. The
end-to-end "no patient text reaches Groq" conversation lives in `test_model_turn.py`;
this file goes at the types underneath it, because a passing end-to-end test only tells
you the paths that exist right now behave -- it says nothing about the path someone adds
next month.

So: a raw turn cannot become a `Restatement`, a `Restatement` cannot be conjured, an
`OutboundPayload` cannot be built from anything else, and `privacy/gate.py` is the only
module in `ai_server/` that constructs the wire types at all.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from ai_server.llm.general_knowledge import GeneralKnowledgeService
from ai_server.llm.tools import AskGeneralKnowledgeCall, ToolSurfaceError, parse_tool_call
from ai_server.privacy.gate import (
    GENERAL_KNOWLEDGE_SYSTEM_PROMPT,
    LOCAL_CORRECTION,
    MAX_RESTATEMENT_CHARACTERS,
    OutboundPayload,
    PrivacyGate,
    Restatement,
    RestatementError,
    mint_restatement,
)

PATIENT_TURN = "I'm at 88 Larch Street and my MRN is MRN-889900 -- is that normal?"
RESTATEMENT = "What is a routine physical examination?"


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture(scope="module")
def gate() -> PrivacyGate:
    """The real analyzer. Presidio is never mocked here -- a stubbed detector would make
    every rejection assertion in this file vacuous."""
    return PrivacyGate.create()


def a_call(restatement: str = RESTATEMENT) -> AskGeneralKnowledgeCall:
    """A parsed tool call, produced the way production produces one."""
    call = parse_tool_call(
        '{"tool":"ask_general_knowledge","arguments":{"restatement":%s}}'
        % _json_string(restatement)
    )
    assert isinstance(call, AskGeneralKnowledgeCall)
    return call


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)


class RecordingClient:
    """Stands in for Groq and records every payload that would leave this process."""

    def __init__(self, answer: str = "An annual check-up.") -> None:
        self.answer = answer
        self.calls: list[OutboundPayload] = []

    async def complete(self, payload: OutboundPayload) -> str:
        self.calls.append(payload)
        return self.answer


# --- AC1: an outbound payload cannot be built from a raw turn -----------------------


def test_a_restatement_cannot_be_constructed_from_a_string() -> None:
    """The mint is the whole mechanism. If this ever stops raising, the type stops
    carrying the guarantee and every other test in this file becomes decorative."""
    with pytest.raises(RestatementError):
        Restatement(PATIENT_TURN)

    with pytest.raises(RestatementError):
        Restatement(RESTATEMENT)


def test_the_mint_refuses_anything_that_is_not_a_parsed_tool_call() -> None:
    """A patient turn is a `str`, and there is no widening from `str` to the mint's
    parameter type -- which is the point: this is a type error, not a policy check."""
    for not_a_call in (PATIENT_TURN, {"restatement": PATIENT_TURN}, None, 42):
        with pytest.raises(RestatementError):
            mint_restatement(not_a_call)  # type: ignore[arg-type]


def test_an_outbound_payload_cannot_be_built_from_a_raw_patient_turn() -> None:
    """AC1, stated as directly as the language allows.

    `for_question` is the only constructor, and the only thing it accepts is a minted
    restatement. There is no argument here into which `ChatTurnRequest.message` fits.
    """
    for not_a_restatement in (PATIENT_TURN, RESTATEMENT, None):
        with pytest.raises(RestatementError):
            OutboundPayload.for_question(not_a_restatement)  # type: ignore[arg-type]


def test_a_minted_restatement_produces_the_two_messages_this_codebase_authored() -> None:
    payload = OutboundPayload.for_question(mint_restatement(a_call()))

    assert payload.messages[0].role == "system"
    assert payload.messages[0].content == GENERAL_KNOWLEDGE_SYSTEM_PROMPT
    assert payload.messages[1].role == "user"
    assert payload.messages[1].content == RESTATEMENT
    # The screened string is the restatement, not the constant.
    assert payload.user_message_content() == RESTATEMENT


def test_the_caller_cannot_supply_the_system_message() -> None:
    """The system message is not screened (see `user_message_content`'s docstring), so
    a caller-supplied one would be an unscreened outbound channel. `for_question` takes
    no such argument, and this pins that it never grows one."""
    with pytest.raises(TypeError):
        OutboundPayload.for_question(  # type: ignore[call-arg]
            mint_restatement(a_call()), system_prompt=PATIENT_TURN
        )


def test_the_restatement_length_is_bounded_at_the_boundary_itself() -> None:
    """Re-checked here rather than assumed from the tool schema. This is the last code
    that runs before the wire, and `MAX_RESTATEMENT_CHARACTERS` is its own bound."""
    over_length = "a" * (MAX_RESTATEMENT_CHARACTERS + 1)

    # The published schema refuses it first...
    with pytest.raises(ToolSurfaceError):
        a_call(over_length)

    # ...and the mint refuses it independently, on a call that somehow got past that.
    forged = AskGeneralKnowledgeCall.model_construct(
        tool="ask_general_knowledge",
        arguments=type("_A", (), {"restatement": over_length})(),
    )
    with pytest.raises(RestatementError):
        mint_restatement(forged)


def test_only_the_privacy_gate_constructs_the_wire_types() -> None:
    """The half of AC1 that Python's type system cannot carry, carried by a test.

    A Pydantic model's constructor cannot be made private, so "an outbound payload is
    only ever built in one place" is asserted by reading the tree instead. This is what
    stops a future caller from bypassing `for_question` and hand-building an
    `OutboundPayload` around a patient's message -- the reviewer's rule becomes a
    failing test.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for source in sorted(root.rglob("*.py")):
        if source.parent.name == "tests" or source.name == "gate.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"OutboundPayload", "OutboundMessage"}:
                    offenders.append(f"{source.relative_to(root)}:{node.lineno}")

    assert offenders == []


# --- AC3: Presidio screens every constructed payload, and rejects ------------------


def test_a_clean_restatement_is_sent_and_answered(gate: PrivacyGate) -> None:
    client = RecordingClient()

    result = run(GeneralKnowledgeService(gate, client).ask(a_call()))

    assert result.outcome == "answered"
    assert result.answer == "An annual check-up."
    assert result.asked == RESTATEMENT
    assert [payload.user_message_content() for payload in client.calls] == [RESTATEMENT]


@pytest.mark.parametrize(
    "restatement",
    [
        "Is a callback on 555-555-5555 normal after bloodwork?",
        "What does the record MRN-889900 mean?",
        "Is the identifier OE-1234ABCD a patient number?",
        "What does healthcare identifier: NPI-1234567890 refer to?",
    ],
)
def test_a_phi_bearing_restatement_is_rejected_and_the_client_is_never_called(
    gate: PrivacyGate, restatement: str
) -> None:
    """The seeded-values test AC3 asks for, at the service boundary.

    Each of these is a *restatement* -- the local model's own output, already past the
    structural control. Presidio is the second, independent control D4 puts here
    precisely for the case where the first one was satisfied and something came over
    anyway.
    """
    client = RecordingClient()

    result = run(GeneralKnowledgeService(gate, client).ask(a_call(restatement)))

    assert result.outcome == "withheld"
    assert result.answer is None
    assert client.calls == []


def test_rejection_never_scrubs_and_never_retries(gate: PrivacyGate) -> None:
    """ADR-5, as a distinct claim from "it was rejected".

    A scrubbing gate would send a redacted payload; a repairing one would send a second.
    Neither is allowed: the count is zero, not "one, cleaned up".
    """
    client = RecordingClient()
    flagged = "Is a callback on 555-555-5555 normal after bloodwork?"

    result = run(GeneralKnowledgeService(gate, client).ask(a_call(flagged)))

    assert client.calls == []
    assert result.asked == flagged
    # The dispatcher's local correction is not passed off as the model's answer.
    assert result.answer is None
    assert result.answer != LOCAL_CORRECTION


# --- AC6: Groq's failures stay inside this one capability --------------------------


def test_an_unavailable_external_model_is_reported_rather_than_raised(gate: PrivacyGate) -> None:
    """AC6 at the service boundary. Anything escaping here would fail the whole turn,
    including the parts of it that are entirely local."""
    from ai_server.llm.groq import GroqUnavailableError

    class FailingClient:
        async def complete(self, payload: OutboundPayload) -> str:
            raise GroqUnavailableError("Groq is unavailable")

    result = run(GeneralKnowledgeService(gate, FailingClient()).ask(a_call()))

    assert result.outcome == "unavailable"
    assert result.answer is None
    assert result.asked == RESTATEMENT


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
