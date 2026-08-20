"""Reject sensitive text before an external model client can receive it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

LOCAL_CORRECTION = "Please remove personal or health information and try again."
_PINNED_MODEL = "openai/gpt-oss-120b"


class OutboundMessage(BaseModel):
    """One allowed message in an external model request."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=4_000)


class OfficeHours(BaseModel):
    """One anonymous office-hours interval."""

    model_config = ConfigDict(extra="forbid")

    day_of_week: str = Field(pattern="^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$")
    opens_at: str = Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    closes_at: str = Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")


class Closure(BaseModel):
    """One office closure interval without patient or provider identifiers."""

    model_config = ConfigDict(extra="forbid")

    starts_at: AwareDatetime
    ends_at: AwareDatetime


class AnonymousSlot(BaseModel):
    """A short-lived slot token that has no OpenEMR identifier."""

    model_config = ConfigDict(extra="forbid")

    slot_token: str = Field(pattern="^slot_[A-Za-z0-9_-]{1,64}$")
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class SchedulingContext(BaseModel):
    """Approved, non-patient scheduling context for a model request."""

    model_config = ConfigDict(extra="forbid")

    current_datetime: AwareDatetime
    timezone: str = Field(pattern="^[A-Za-z]+/[A-Za-z_]+$")
    office_hours: list[OfficeHours] = Field(default_factory=list)
    closures: list[Closure] = Field(default_factory=list)
    open_slots: list[AnonymousSlot] = Field(default_factory=list)


class SchedulingRules(BaseModel):
    """Approved scheduling behavior controls."""

    model_config = ConfigDict(extra="forbid")

    minimum_booking_notice_minutes: int = Field(ge=0, le=43_200)
    booking_enabled: bool
    rescheduling_enabled: bool
    cancellation_enabled: bool


class ResponseFormat(BaseModel):
    """The only supported structured response contract."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["json_schema"]
    schema_version: Literal["1"]


class OutboundPayload(BaseModel):
    """The architecture-approved external request shape."""

    model_config = ConfigDict(extra="forbid")

    model: Literal[_PINNED_MODEL]
    messages: list[OutboundMessage] = Field(min_length=2, max_length=2)
    scheduling_context: SchedulingContext
    scheduling_rules: SchedulingRules
    response_format: ResponseFormat

    @model_validator(mode="after")
    def validate_message_order(self) -> OutboundPayload:
        """Require the fixed system/user message ordering from the architecture contract."""
        system_message, user_message = self.messages
        if system_message.role != "system" or user_message.role != "user":
            raise ValueError("messages must contain a system message followed by a user message")
        return self

    def user_message_content(self) -> str:
        """Return the one string that can ever contain patient-supplied text.

        The system message is a fixed, developer-authored constant (SYSTEM_PROMPT in
        ai_server/app/chat.py) that never carries runtime patient data, so it is not a
        privacy-gate target -- screening it anyway false-positives on ordinary
        scheduling vocabulary (confirmed live: Presidio's DATE_TIME recognizer flags
        "hours" in "office hours"), which would reject every request unconditionally.
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
    """Enforce payload validation and local privacy rejection before model dispatch."""

    def __init__(self, gate: PrivacyGate, client: ExternalModelClient) -> None:
        self._gate = gate
        self._client = client

    async def dispatch(self, payload: OutboundPayload) -> DispatchResult:
        """Reject local PHI/PII or send only an approved, validated payload."""
        if self._gate.has_sensitive_text(payload.user_message_content()):
            return DispatchResult(accepted=False, content=LOCAL_CORRECTION)
        return DispatchResult(accepted=True, content=await self._client.complete(payload))
