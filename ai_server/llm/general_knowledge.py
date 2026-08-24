"""The `ask_general_knowledge` path: restate, screen, send -- or say why not.

TICK-064, from `docs/LOCAL_LLM_SPEC.md` D13 and D14. This is the only place in the
application that reaches an external model, and it is reachable only from one branch of
`ModelTurnService._execute`.

**Three things happen here, in this order, and none of them is optional.** The model's
parsed `ask_general_knowledge` call is minted into a `Restatement`
(`ai_server.privacy.gate.mint_restatement`); the restatement is composed into the one
outbound shape by `OutboundPayload.for_question()`, which supplies both messages itself;
and `OutboundDispatcher` screens that constructed payload with Presidio before the
client is called at all (D4, ADR-5). There is no argument to any of these steps through
which the patient's typed turn could arrive.

**Every outcome is reported, never papered over.** A withheld question and an
unreachable Groq are distinct results rather than a shrug, because answering "from
nothing" would be the failure mode worth avoiding: the patient asked something and is
entitled to know it was not asked. `asked` is returned alongside the answer for the same
reason -- the restatement is otherwise invisible to the patient, which the spec records
as D14's standing risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from ai_server.llm.provider import LlmUnavailableError
from ai_server.llm.tools import AskGeneralKnowledgeCall
from ai_server.privacy.gate import (
    ExternalModelClient,
    OutboundDispatcher,
    OutboundPayload,
    PrivacyGate,
    mint_restatement,
)


@dataclass(frozen=True)
class GeneralKnowledgeAnswer:
    """What one outbound question produced, and what was asked to produce it.

    `asked` is always the exact text that was put on the wire (or would have been, when
    it was withheld), so a caller can show the patient the question asked on their
    behalf without reconstructing it.
    """

    asked: str
    answer: str | None
    outcome: Literal["answered", "withheld", "unavailable"]


class GeneralKnowledgeService:
    """Answer a restated question through the screened outbound boundary."""

    def __init__(self, gate: PrivacyGate, client: ExternalModelClient) -> None:
        self._dispatcher = OutboundDispatcher(gate, client)

    async def ask(self, call: AskGeneralKnowledgeCall) -> GeneralKnowledgeAnswer:
        """Send this turn's restatement, or report why nothing was sent."""
        restatement = mint_restatement(call)
        payload = OutboundPayload.for_question(restatement)
        try:
            result = await self._dispatcher.dispatch(payload)
        # `LlmUnavailableError` rather than `GroqUnavailableError`: an unreachable
        # external model degrades this one capability (AC6) and must not escape as a 500
        # through a turn whose other tools are entirely local and still working.
        except (LlmUnavailableError, httpx.HTTPError):
            return GeneralKnowledgeAnswer(restatement.text, None, "unavailable")
        if not result.accepted:
            return GeneralKnowledgeAnswer(restatement.text, None, "withheld")
        return GeneralKnowledgeAnswer(restatement.text, result.content, "answered")
