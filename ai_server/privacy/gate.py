"""The only door out of this deployment, and the only text allowed through it.

TICK-064, from `docs/LOCAL_LLM_SPEC.md` D3, D4, D13 and D14. Before this, `chat.py`
built an outbound payload with `OutboundMessage(role="user", content=message)` -- the
patient's raw typed text -- and Presidio scanning that text was the *only* control. A
detector fails open on whatever it does not recognise, which is the same unbounded-cases
problem as the deterministic parsers, relocated.

**The boundary is structural now (D3).** The outbound user message is no longer a
parameter. `OutboundPayload.for_question()` is the only constructor of an outbound
payload, it takes a `Restatement` rather than a string, and a `Restatement` cannot be
built from a string at all: `mint_restatement()` accepts only a parsed
`AskGeneralKnowledgeCall`, so a `ChatTurnRequest.message` has no route to this module.
Both messages on the wire are composed here -- the system message from
`GENERAL_KNOWLEDGE_SYSTEM_PROMPT`, a constant in this file, and the user message from
the model's own canonical restatement (D14). Every string that leaves is one this
codebase or this codebase's local model produced.

**Presidio is the second control, not the only one (D4).** `OutboundDispatcher` screens
every constructed payload before egress and, per ADR-5, rejects rather than scrubs: a
flagged payload is never sent in any form. A restatement could in principle carry
something over from the turn it restates, and that is exactly what this catches.

**What was removed.** `SchedulingContext`, `SchedulingRules`, `OfficeHours`, `Closure`,
`AnonymousSlot` and `AnonymousAppointment` were the outbound scheduling-planning
contract. D13 moves scheduling planning to the local model, so nothing outbound carries
it and the types are gone rather than left looking load-bearing. The slot and
appointment token regexes they held now live only in `ai_server.llm.tools`, so there is
one copy rather than two.

Python cannot make a Pydantic model's constructor private, so the "no raw turn can
become a payload" property is carried by two things that a reader can check: the
`Restatement` mint above, which no patient string can pass, and
`test_general_knowledge.py`'s guard that this module is the only one in `ai_server/`
which constructs `OutboundPayload` or `OutboundMessage` at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_server.llm.tools import AskGeneralKnowledgeCall

LOCAL_CORRECTION = "Please remove personal or health information and try again."
_PINNED_MODEL = "openai/gpt-oss-120b"

# The outbound system message, in full. A fixed developer-authored constant rather than
# anything assembled per turn: `user_message_content()` screens only the user message
# (see its docstring for why the system message cannot be screened), so a system message
# built from runtime data would be an unscreened channel. There is deliberately no
# parameter through which a caller can add to it.
GENERAL_KNOWLEDGE_SYSTEM_PROMPT = (
    "You are answering one general-knowledge question on behalf of a patient portal. "
    "The question carries no patient context and you have none: do not ask for any, do "
    "not assume any, and do not invent any. Answer in plain language in at most a short "
    "paragraph. If the question needs someone's own medical history or record to answer, "
    "say so and suggest contacting the clinic instead of guessing."
)

# This module's own bound on outbound text, re-checked rather than assumed.
# `AskGeneralKnowledgeArguments.restatement` already caps at the same length; the
# duplication is the point, since this is the last check before the wire.
MAX_RESTATEMENT_CHARACTERS = 500


class RestatementError(Exception):
    """Raised when outbound text was not minted from a model restatement."""


class Restatement:
    """A canonical, context-free question this system composed (D14).

    Not constructible: `Restatement("anything")` raises. The one mint is
    `mint_restatement()`, whose parameter is a parsed `AskGeneralKnowledgeCall` and not
    a string -- so there is no widening by which a patient's typed turn becomes one of
    these, and therefore no code path by which it becomes outbound text.
    """

    __slots__ = ("text",)

    def __init__(self, text: str, *, mint: object = None) -> None:
        if mint is not _MINT:
            raise RestatementError(
                "a Restatement can only be minted by ai_server.privacy.gate."
                "mint_restatement() from a parsed ask_general_knowledge tool call"
            )
        self.text = text


_MINT = object()


def mint_restatement(call: AskGeneralKnowledgeCall) -> Restatement:
    """Mint the one piece of text this turn is allowed to send outside the deployment.

    The parameter type does the work: the only value that satisfies it is a tool call
    the local model emitted under the published schema and `parse_tool_call()` accepted.
    Nothing about a `ChatTurnRequest`, a transcript entry, or a bare string can be passed
    here, which is what makes "no patient-typed text egresses" a property of the types
    rather than a rule someone has to remember.

    The model could still restate a question using words the patient used -- D14 says so
    plainly -- which is why `OutboundDispatcher` screens the result before it goes
    anywhere. This mint is the structural control; Presidio is the detective one.
    """
    if not isinstance(call, AskGeneralKnowledgeCall):
        raise RestatementError("only a parsed ask_general_knowledge call can be restated")
    text = call.arguments.restatement
    if not text or len(text) > MAX_RESTATEMENT_CHARACTERS:
        raise RestatementError("a restatement must be between 1 and 500 characters")
    return Restatement(text, mint=_MINT)


class OutboundMessage(BaseModel):
    """One allowed message in an external model request."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=4_000)


class OutboundPayload(BaseModel):
    """The architecture-approved external request shape.

    Both messages are composed by `for_question()` from this module's own constants and
    a minted `Restatement`. No caller supplies either one.
    """

    model_config = ConfigDict(extra="forbid")

    model: Literal[_PINNED_MODEL]
    messages: list[OutboundMessage] = Field(min_length=2, max_length=2)

    @classmethod
    def for_question(cls, restatement: Restatement) -> OutboundPayload:
        """Build the one outbound shape this codebase sends: a restated question."""
        if not isinstance(restatement, Restatement):
            raise RestatementError("an outbound payload can only be built from a Restatement")
        return cls(
            model=_PINNED_MODEL,
            messages=[
                OutboundMessage(role="system", content=GENERAL_KNOWLEDGE_SYSTEM_PROMPT),
                OutboundMessage(role="user", content=restatement.text),
            ],
        )

    @model_validator(mode="after")
    def validate_message_order(self) -> OutboundPayload:
        """Require the fixed system/user message ordering from the architecture contract."""
        system_message, user_message = self.messages
        if system_message.role != "system" or user_message.role != "user":
            raise ValueError("messages must contain a system message followed by a user message")
        return self

    def user_message_content(self) -> str:
        """Return the one string Presidio screens: the restatement about to be sent.

        The system message is `GENERAL_KNOWLEDGE_SYSTEM_PROMPT`, a fixed constant in this
        module that never carries runtime data, so it is not a privacy-gate target --
        screening it anyway false-positives on ordinary vocabulary (confirmed live:
        Presidio's DATE_TIME recognizer flags "hours" in "office hours"), which would
        reject every request unconditionally.
        """
        return self.messages[1].content


class ExternalModelClient(Protocol):
    """The narrow boundary through which approved requests leave this process."""

    async def complete(self, payload: OutboundPayload) -> str:
        """Send an already validated payload to the configured external model."""


@dataclass(frozen=True)
class DispatchResult:
    """A local correction or external-model response, without match details."""

    accepted: bool
    content: str


class PrivacyGate:
    """A pinned local Presidio analyzer with project-specific healthcare recognition."""

    def __init__(self, analyzer: AnalyzerEngine) -> None:
        self._analyzer = analyzer

    @classmethod
    def create(cls) -> PrivacyGate:
        """Create the one local analyzer used by the application process."""
        analyzer = AnalyzerEngine()
        analyzer.registry.add_recognizer(
            PatternRecognizer(
                supported_entity="OPENEMR_IDENTIFIER",
                patterns=[
                    Pattern(
                        "openemr_identifier",
                        r"\b(?:OE|SYN|OPENEMR)[-:][A-Z0-9-]{4,}\b",
                        1.0,
                    )
                ],
            )
        )
        analyzer.registry.add_recognizer(
            PatternRecognizer(
                supported_entity="MEDICAL_RECORD_NUMBER",
                patterns=[Pattern("medical_record_number", r"\bMRN[-:\s]?\d{4,}\b", 1.0)],
            )
        )
        analyzer.registry.add_recognizer(
            PatternRecognizer(
                supported_entity="HEALTHCARE_IDENTIFIER",
                patterns=[
                    Pattern("healthcare_identifier", r"\bNPI[-:\s]?\d{10}\b", 1.0),
                    Pattern(
                        "healthcare_record_value",
                        r"(?i)\b(?:patient\s+record\s+value|medical\s+record|healthcare\s+identifier)\s*:\s*\S+",
                        1.0,
                    ),
                ],
            )
        )
        return cls(analyzer)

    def has_sensitive_text(self, text: str) -> bool:
        """Return whether Presidio's built-in or healthcare recognizers find a match."""
        return bool(self._analyzer.analyze(text=text, language="en"))


class OutboundDispatcher:
    """Enforce payload validation and local privacy rejection before model dispatch.

    The one production egress door since TICK-064 -- `GroqWorkflow` used to carry a
    second, separate copy of this check and has been deleted, so there is now exactly
    one place where something can leave and exactly one place that screens it.
    """

    def __init__(self, gate: PrivacyGate, client: ExternalModelClient) -> None:
        self._gate = gate
        self._client = client

    async def dispatch(self, payload: OutboundPayload) -> DispatchResult:
        """Reject local PHI/PII or send only an approved, validated payload.

        ADR-5: reject, never scrub. A flagged payload is not repaired and re-sent in a
        redacted form -- the client is not called at all, and the caller is told the
        question was withheld rather than being answered from nothing.
        """
        if self._gate.has_sensitive_text(payload.user_message_content()):
            return DispatchResult(accepted=False, content=LOCAL_CORRECTION)
        return DispatchResult(accepted=True, content=await self._client.complete(payload))
