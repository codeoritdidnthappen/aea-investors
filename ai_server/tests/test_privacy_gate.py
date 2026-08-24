from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_server.fixtures.generator import generate
from ai_server.llm.tools import AskGeneralKnowledgeCall
from ai_server.privacy.gate import (
    GENERAL_KNOWLEDGE_SYSTEM_PROMPT,
    LOCAL_CORRECTION,
    DispatchResult,
    OutboundDispatcher,
    OutboundPayload,
    PrivacyGate,
    mint_restatement,
)


class CapturingModelClient:
    def __init__(self) -> None:
        self.calls: list[OutboundPayload] = []

    async def complete(self, payload: OutboundPayload) -> str:
        self.calls.append(payload)
        return "approved response"


@pytest.fixture(scope="module")
def privacy_gate() -> PrivacyGate:
    return PrivacyGate.create()


def approved_payload(prompt: str = "What does a routine physical involve?") -> OutboundPayload:
    """Build the payload under test the only way this codebase can build one.

    TICK-064 removed `scheduling_context`/`scheduling_rules`/`response_format` (D13) and,
    more to the point, removed the ability to hand `OutboundPayload` a user message at
    all. `prompt` here is a *restatement* -- it can only arrive through the model's
    published `ask_general_knowledge` argument, which is what makes "the patient's words
    never egress" structural rather than a convention this helper happens to follow.
    """
    call = AskGeneralKnowledgeCall.model_validate(
        {"tool": "ask_general_knowledge", "arguments": {"restatement": prompt}}
    )
    return OutboundPayload.for_question(mint_restatement(call))


@pytest.mark.parametrize(
    "prompt",
    ["My phone number is 555-555-5555.", "My medical license is A1234567."],
)
def test_ticket_009_rejects_builtin_pii_and_medical_data_without_calling_model(
    privacy_gate: PrivacyGate, prompt: str
) -> None:
    client = CapturingModelClient()
    result = asyncio.run(
        OutboundDispatcher(privacy_gate, client).dispatch(approved_payload(prompt))
    )

    assert result == DispatchResult(accepted=False, content=LOCAL_CORRECTION)
    assert client.calls == []


@pytest.mark.parametrize(
    "prompt", ["My MRN is MRN-123456.", "Use OE-1234ABCD.", "My NPI: 1234567890."]
)
def test_ticket_009_rejects_custom_healthcare_identifiers_without_calling_model(
    privacy_gate: PrivacyGate, prompt: str
) -> None:
    client = CapturingModelClient()
    result = asyncio.run(
        OutboundDispatcher(privacy_gate, client).dispatch(approved_payload(prompt))
    )

    assert result.accepted is False
    assert client.calls == []


def test_ticket_009_allows_only_the_approved_payload_shape(privacy_gate: PrivacyGate) -> None:
    client = CapturingModelClient()
    result = asyncio.run(OutboundDispatcher(privacy_gate, client).dispatch(approved_payload()))

    assert result == DispatchResult(accepted=True, content="approved response")
    assert len(client.calls) == 1

    with pytest.raises(ValidationError):
        OutboundPayload.model_validate({**approved_payload().model_dump(), "patient_id": "p-123"})

    invalid_messages = approved_payload().model_dump()
    invalid_messages["messages"] = list(reversed(invalid_messages["messages"]))
    with pytest.raises(ValidationError, match="system message followed by a user message"):
        OutboundPayload.model_validate(invalid_messages)

    # The system message is this codebase's constant, not something a caller chose.
    assert approved_payload().messages[0].content == GENERAL_KNOWLEDGE_SYSTEM_PROMPT


def test_ticket_009_golden_corpus_rejects_every_seeded_sensitive_value(
    privacy_gate: PrivacyGate, tmp_path: Path
) -> None:
    generate(seed="ticket-009", count=6, output=tmp_path)
    corpus = json.loads(
        (tmp_path / "evaluation" / "privacy-corpus.json").read_text(encoding="utf-8")
    )

    allowed = [
        value
        for value in corpus
        if not privacy_gate.has_sensitive_text(f"Patient record value: {value}")
    ]

    assert allowed == []


def test_ticket_009_never_logs_prompt_or_recognition_result(
    privacy_gate: PrivacyGate, caplog: pytest.LogCaptureFixture
) -> None:
    sensitive_prompt = "My phone number is 555-555-5555."
    client = CapturingModelClient()

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            OutboundDispatcher(privacy_gate, client).dispatch(approved_payload(sensitive_prompt))
        )

    assert result.accepted is False
    assert sensitive_prompt not in caplog.text
    assert "PHONE_NUMBER" not in caplog.text
