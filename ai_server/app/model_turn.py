"""The local model as the front door for every chat turn.

TICK-063, from `docs/LOCAL_LLM_SPEC.md` D9, D10 and D16. Before this, `chat_turn`
steered PHI-bearing turns *away* from the model: `onboarding_mode()` and
`address_update_mode()` matched the patient's words against phrasings and intercepted
them, and everything else went to Groq. That is inverted here. Every message now goes to
the local model first -- which is allowed to see patient data (D2) -- and the model's one
tool call selects what happens. TICK-065 then deleted `ai_server/app/onboarding_chat.py`
and `ai_server/app/address_chat.py`, so this is not merely the preferred path, it is the
only one.

**What pattern matching survives in the turn path, and why (TICK-065 AC2).** Two things,
both named here so neither is left to be discovered:

- `_accept_upload` runs before the model. Its trigger is the request's `image_base64`
  field being present -- a structural fact about the HTTP request, not a reading of what
  the patient typed. It cannot fire on any wording.
- `_consent_given` reads one boolean out of the chat page's own machine-generated action
  envelope (`json.loads`, one key). FR-21 requires explicit consent before an image is
  read from disk, and consent inferred from phrasing would not be consent.

Neither decides what the turn is about; both are inputs the model is then told about.
`ai_server/onboarding/triggers.py` is the deliberate third case: its `detect_distress`
substring corpus is intent-shaped, and after TICK-065 no production code calls it at all,
because its only two callers were the deleted modules. It is left in the tree rather than
deleted because `evidence/TICK-067/FOLLOW_UP_TICKETS.md` owns re-wiring approved
supportive content onto every turn, and it must survive to be re-wired. Until then it is
not in the turn path.

**The model's judgement selects an action; it never gates egress (D10).** One tool,
`ask_general_knowledge`, does reach an external model as of TICK-064 -- but nothing in
this module composes what is sent. It hands the parsed call to
`ai_server.llm.general_knowledge`, which mints a `Restatement` from the model's own
schema'd field, has `OutboundPayload.for_question()` compose both wire messages from
that plus a constant, and has Presidio screen the result before it goes (D3, D4, D14).
The patient's typed `message` is not a parameter to any of that and has no route into
it. So a wrong routing decision here picks the wrong action for the turn; it still
cannot place patient data into an outbound request.

**Model proposes, code disposes (D6), through the existing doors.** The call is parsed
by `ai_server.llm.tools.parse_tool_call`, every writing tool's fields go through
`ai_server.llm.validation.validate_write`, the patient is shown
`confirmation_prompt()`'s render of the *validated* values, and the backing service is
finally called with `executable_arguments()`'s output -- the validator's values, never
the model's. This module adds no way around any of those.

**How a confirmation is recognised (AC5).** Not by a keyword. When a change is pending,
the model is told what was read back and what agreeing or correcting means
(`ai_server.llm.prompt`); the turn is a confirmation exactly when the model re-emits the
same writing tool and the validator produces the same values it produced last turn. A
patient who says "actually, make it 2004" therefore yields a *different* validated
write, which is read back for confirmation in its turn rather than saved -- and a
patient who changes the subject abandons the pending change without saving it. There is
no phrase list on either side of that.

**The turn's shape (D16).** The routing inference has to finish before anything can be
said at all, so the pause before the first token is real. It is measured and reported as
`TurnMetrics.routing_seconds` rather than hidden, and the reply then streams to the
browser as the existing chat page already expects.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Mapping, Protocol, Sequence

from ai_server.app.chat import ASSISTANT_UNAVAILABLE_RESPONSE
from ai_server.app.conversation import ConversationState, ConversationStore
from ai_server.llm.general_knowledge import GeneralKnowledgeService
from ai_server.llm.prompt import render_turn_messages
from ai_server.llm.provider import LlmUnavailableError
from ai_server.llm.tools import (
    ToolSurfaceError,
    envelope_json_schema,
    executable_arguments,
    parse_tool_call,
)
from ai_server.llm.validation import (
    ValidatedWrite,
    WriteContext,
    confirmation_prompt,
    validate_write,
    write_authority,
)
from ai_server.ocr.service import (
    ConsentRequiredError,
    ExtractedIdentity,
    InvalidUploadError,
    OcrService,
)
from ai_server.onboarding.fields import Address
from ai_server.onboarding.flow import FieldCheckpointRejected, OnboardingCursor, OnboardingFlow
from ai_server.openemr.adapter import OpenEmrRequestError
from ai_server.openemr.demographics import (
    IdentityNotConfirmedError,
    OpenEmrDemographicsAdapter,
    confirm_address,
    confirm_demographic_fields,
)
from ai_server.scheduling.appointments import AppointmentDiscoveryService
from ai_server.scheduling.booking import AppointmentRequest, BookingService, SlotBookingError
from ai_server.scheduling.cancel import (
    AppointmentAlreadyCancelledError,
    AppointmentNotFoundError,
    CancellationService,
    StaleAppointmentTokenError,
)
from ai_server.scheduling.slots import SlotDiscoveryService

logger = logging.getLogger(__name__)

# Groq is not configured, or did not answer. Only general knowledge is affected: every
# other tool on the surface is backed by a local service and keeps working, so this says
# what is unavailable rather than reporting a chat-wide outage (AC6).
GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE = (
    "I could not look that one up just now, so I have no answer for you. Everything "
    "else still works -- your appointments, your details, and your intake questions -- "
    "or you can contact the clinic directly."
)

# Presidio flagged the restatement this codebase was about to send (ADR-5: reject, never
# scrub). Nothing was sent, and this says so instead of answering anyway from a model
# that was never asked -- an invented answer to a withheld question is the worse
# failure. The restatement is quoted for the same reason `_ask_general_knowledge` quotes
# an answered one: the patient can see what was withheld and rephrase it themselves.
GENERAL_KNOWLEDGE_WITHHELD_RESPONSE = (
    "I was going to look this up for you:\n\n{asked}\n\nI did not send it, because it "
    "looked like it contained personal or health information and that must not leave "
    "the clinic. Nothing was sent and I have no answer. Please ask again without any "
    "personal details in it."
)

# What was asked on the patient's behalf, shown with the answer (AC4). The restatement
# is composed by the model and is otherwise invisible, which LOCAL_LLM_SPEC records as
# D14's standing risk: a restatement that drops or distorts the question yields a
# correct-looking answer to something the patient did not ask. Showing it is what makes
# that visible rather than silent, so the patient can tell that it was answered.
GENERAL_KNOWLEDGE_ANSWER_TEMPLATE = 'I looked this up for you -- "{asked}":\n\n{answer}'

NO_ACTION_RESPONSE = (
    "Sorry -- I could not reach your record just now, so nothing was done. Please try "
    "again in a moment."
)

NO_SLOTS_RESPONSE = (
    "I don't have any open appointment times to offer right now. Please contact the "
    "clinic directly and they can find you one."
)

NO_APPOINTMENTS_RESPONSE = "You have no upcoming appointments booked."

UPLOAD_CONSENT_REQUIRED_RESPONSE = (
    "I did not have your consent to read that image, so it was not read and nothing was "
    "saved. Please tick the consent box and attach it again."
)

UPLOAD_UNREADABLE_RESPONSE = (
    "I could not read that image, so nothing was taken from it. Please attach a clearer "
    "photo, or just type your details and I'll record them."
)

UNKNOWN_UPLOAD_RESPONSE = (
    "I don't have a document to read for you, so nothing was read. Please attach the "
    "photo again if you'd like me to."
)

# How the reply is handed to the browser. The chat page appends every chunk it reads to
# the same transcript entry, so the split is cosmetic -- what matters is that the
# response body is not one blocking write (D16).
_STREAM_CHUNK_CHARACTERS = 60


class ToolCallClient(Protocol):
    """The one thing the turn needs from a model runtime: a routing inference.

    Deliberately narrower than `GroqClient`. `HttpLocalModelClient` satisfies it, and
    nothing that speaks to an external model does -- the type itself records that this
    path calls the local model and only the local model.
    """

    async def tool_call(
        self, messages: Sequence[Mapping[str, str]], *, schema: Mapping[str, Any]
    ) -> str:
        """Return the model's raw response for one turn, unparsed."""
        ...


@dataclass(frozen=True)
class TurnMetrics:
    """What one turn cost and what it decided (AC3).

    `routing_seconds` is D16's pause, stated rather than hidden: the wall-clock time
    between sending the turn to the model and having a tool call to act on, which is
    exactly the interval during which the patient sees nothing.
    """

    routing_seconds: float
    tool: str | None
    outcome: str


def log_turn_metrics(metrics: TurnMetrics) -> None:
    """Record one turn's routing pause and outcome; never the patient's words.

    `tool` is a published tool name or `None` and `outcome` is one of this module's own
    fixed strings, so nothing the patient or the model wrote can reach the log here.
    """
    logger.info(
        "model turn: routing_seconds=%.3f tool=%s outcome=%s",
        metrics.routing_seconds,
        metrics.tool or "-",
        metrics.outcome,
    )


@dataclass(frozen=True)
class TurnServices:
    """The backing services each tool executes through, or `None` where unconfigured.

    Every one of these already existed; the tool surface is a re-facing of them
    (LOCAL_LLM_SPEC "Tool surface"). An absent service degrades that one capability for
    the turn rather than failing startup, matching `_build_model_turn_service`'s
    tolerance of missing OpenEMR configuration.
    """

    # The one service here that is not local. Absent when Groq is unconfigured, which
    # costs general-knowledge answers and nothing else -- every other field below backs
    # a patient-specific capability that must keep working regardless (AC6).
    general_knowledge: GeneralKnowledgeService | None = None
    slot_discovery: SlotDiscoveryService | None = None
    appointment_discovery: AppointmentDiscoveryService | None = None
    booking: BookingService | None = None
    cancellation: CancellationService | None = None
    appointment_request: AppointmentRequest | None = None
    demographics: OpenEmrDemographicsAdapter | None = None
    onboarding: OnboardingFlow | None = None
    ocr: OcrService | None = None


class CursorStore(Protocol):
    """The two `SessionStore` methods the onboarding position needs.

    Narrowed to a Protocol so this module cannot reach the access token or the patient
    id through it: those are read once per turn by the route and passed in, and are held
    nowhere (TICK-034 AC1).
    """

    def load_cursor(self, handle: str, now: datetime) -> str | None: ...

    def save_cursor(self, handle: str, cursor: str, now: datetime) -> None: ...


@dataclass
class ModelTurnService:
    """Runs one chat turn: ask the model, execute its call, stream the answer."""

    client: ToolCallClient | None
    services: TurnServices = TurnServices()
    cursors: CursorStore | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    conversations: ConversationStore = field(default_factory=ConversationStore)
    metrics: Callable[[TurnMetrics], None] = log_turn_metrics

    def discard(self, handle: str) -> None:
        """Forget this handle's conversation (called by `/api/logout`)."""
        self.conversations.discard(handle)

    async def stream_reply(
        self,
        handle: str,
        message: str,
        image_base64: str | None = None,
        access_token: str | None = None,
        patient_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield this turn's reply, one chunk at a time.

        `access_token`/`patient_id` are the turn's delegated OpenEMR credentials
        (TICK-034 AC1): used to build this turn's context and to make this turn's calls,
        and held nowhere once this generator finishes.
        """
        if self.client is None:
            # D12: with no fallback path, model-server availability *is* chat
            # availability, and the outage is reported honestly rather than looking like
            # a broken feature.
            yield ASSISTANT_UNAVAILABLE_RESPONSE
            return
        now = self.clock()
        state = self.conversations.state(handle, now)
        upload_note = await self._accept_upload(state, message, image_base64, now)
        if upload_note is not None:
            async for chunk in self._answer(state, message, upload_note):
                yield chunk
            return

        started = time.perf_counter()
        try:
            raw = await self.client.tool_call(
                render_turn_messages(
                    message=message,
                    now=now,
                    offered_slots=state.offered_slots,
                    offered_appointments=state.offered_appointments,
                    # `asked` carries the most recent assistant message, so the
                    # transcript stops one short of it: every message reaches the model
                    # exactly once, in order, and the corpus's own framing of the
                    # pending question (prompt rule 6) is preserved.
                    asked=state.asked,
                    pending_confirmation=state.pending_prompt,
                    upload_id=state.upload_id,
                    transcript=state.earlier_turns(),
                ),
                schema=envelope_json_schema(),
            )
        except LlmUnavailableError:
            self.metrics(
                TurnMetrics(time.perf_counter() - started, None, "model_server_unavailable")
            )
            yield ASSISTANT_UNAVAILABLE_RESPONSE
            return
        routing_seconds = time.perf_counter() - started

        try:
            call = parse_tool_call(raw)
        except ToolSurfaceError as exc:
            # A refused call is a turn the chat answers, not an exception that reaches
            # the transcript; the surface owns the sentence the patient sees.
            self.metrics(TurnMetrics(routing_seconds, None, "refused"))
            async for chunk in self._answer(state, message, exc.patient_message):
                yield chunk
            return

        try:
            reply, outcome = await self._execute(call, state, handle, access_token, patient_id, now)
        except ToolSurfaceError as exc:
            reply, outcome = exc.patient_message, "refused"
        self.metrics(TurnMetrics(routing_seconds, call.tool, outcome))
        async for chunk in self._answer(state, message, reply):
            yield chunk

    async def _answer(
        self, state: ConversationState, message: str, reply: str
    ) -> AsyncIterator[str]:
        """Record the exchange and stream the reply (D16).

        The transcript is written here, in one place, so no execution path can answer
        the patient without the next turn's model seeing what was said.

        `asked` is dropped when the reply *is* the pending confirmation, because the
        pending block already quotes it. Setting both would put the same paragraph in the
        system message twice and then a third time as a transcript entry, which is the
        kind of repetition an 8B model reads as emphasis (D11).
        """
        state.record("user", message)
        state.record("assistant", reply)
        state.asked = None if reply == state.pending_prompt else reply
        for chunk in _chunks(reply):
            # A real suspension point per chunk: the route wraps this in a
            # `StreamingResponse`, and without one the whole body would be assembled
            # before anything reached the socket.
            await asyncio.sleep(0)
            yield chunk

    # --- Uploads -------------------------------------------------------------------

    async def _accept_upload(
        self, state: ConversationState, message: str, image_base64: str | None, now: datetime
    ) -> str | None:
        """Take a just-attached document into the OCR store, or report why not.

        Returns `None` when there was nothing to accept or it was accepted, and the
        sentence to answer with when it could not be. An accepted upload is *not* read
        here: its handle goes into the conversation state and the model is told about
        it, so `extract_document_fields` reading it stays the model's decision (D9).

        The trigger is the request's own `image_base64` field being present -- a
        structural fact about the request, not a phrasing (AC1). `consent` is read from
        the page's machine-generated action envelope because FR-21 requires explicit
        consent before anything is read from disk, and consent that was inferred would
        not be consent.
        """
        if image_base64 is None:
            return None
        if self.services.ocr is None:
            return UPLOAD_UNREADABLE_RESPONSE
        try:
            upload_id = self.services.ocr.begin(consent=_consent_given(message), now=now)
        except ConsentRequiredError:
            return UPLOAD_CONSENT_REQUIRED_RESPONSE
        try:
            image = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError):
            self.services.ocr.revoke(upload_id, now)
            return UPLOAD_UNREADABLE_RESPONSE
        try:
            await self.services.ocr.submit(upload_id, image, now)
        except InvalidUploadError:
            self.services.ocr.revoke(upload_id, now)
            return UPLOAD_UNREADABLE_RESPONSE
        state.upload_id = upload_id
        return None

    # --- Executing the model's one call ---------------------------------------------

    async def _execute(
        self,
        call: Any,
        state: ConversationState,
        handle: str,
        access_token: str | None,
        patient_id: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        """Run the model's tool call and return `(reply, outcome)`."""
        if call.tool in _WRITING_TOOLS_HANDLED_HERE:
            return await self._execute_write(call, state, handle, access_token, patient_id, now)
        # Not a writing tool, so whatever change was pending is abandoned: the patient
        # went somewhere else and nothing may be saved behind them (AC5).
        state.clear_pending()
        arguments = executable_arguments(call)
        if call.tool == "reply":
            return str(arguments["message"]), "replied"
        if call.tool == "ask_general_knowledge":
            return await self._ask_general_knowledge(call)
        if call.tool == "find_slots":
            return await self._find_slots(state, access_token, now)
        if call.tool == "list_appointments":
            return await self._list_appointments(state, access_token, patient_id, now)
        # The only remaining read tool on the surface.
        return self._extract_document_fields(state, str(arguments["upload_id"]), now)

    async def _ask_general_knowledge(self, call: Any) -> tuple[str, str]:
        """Ask the external model the question the local model restated (D13, D14).

        `call` is passed through untouched rather than unpacked into a string here. The
        restatement is minted from the parsed call by `ai_server.privacy.gate`, and
        keeping the call whole up to that point is what leaves this module with nothing
        it could substitute the patient's turn for: there is no string parameter on this
        path to put one in.
        """
        if self.services.general_knowledge is None:
            return GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE, "general_knowledge_unavailable"
        result = await self.services.general_knowledge.ask(call)
        if result.outcome == "withheld":
            return (
                GENERAL_KNOWLEDGE_WITHHELD_RESPONSE.format(asked=result.asked),
                "general_knowledge_withheld",
            )
        # Falsy rather than `is None`: an empty answer is an unavailable one. Presenting
        # "I looked this up for you" over nothing would read as a lookup that happened.
        if not result.answer:
            return GENERAL_KNOWLEDGE_UNAVAILABLE_RESPONSE, "general_knowledge_unavailable"
        return (
            GENERAL_KNOWLEDGE_ANSWER_TEMPLATE.format(asked=result.asked, answer=result.answer),
            "general_knowledge_answered",
        )

    async def _find_slots(
        self, state: ConversationState, access_token: str | None, now: datetime
    ) -> tuple[str, str]:
        if self.services.slot_discovery is None or access_token is None:
            return NO_SLOTS_RESPONSE, "unavailable"
        try:
            offered = await self.services.slot_discovery.open_slots(access_token, now)
        except OpenEmrRequestError:
            return NO_ACTION_RESPONSE, "unavailable"
        # Recorded exactly as issued. Re-listing mints fresh tokens and invalidates
        # these, so `WriteContext` must be built from what the model was actually shown.
        state.offered_slots = tuple(offered)
        if not offered:
            return NO_SLOTS_RESPONSE, "listed"
        lines = "\n".join(f"- {_window(slot.starts_at, slot.ends_at)}" for slot in offered)
        return (
            f"Here are the appointment times I can offer:\n{lines}\n\nTell me which one "
            "you would like and I'll read it back before booking anything."
        ), "listed"

    async def _list_appointments(
        self,
        state: ConversationState,
        access_token: str | None,
        patient_id: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        discovery = self.services.appointment_discovery
        if discovery is None or access_token is None or patient_id is None:
            return NO_ACTION_RESPONSE, "unavailable"
        try:
            offered = await discovery.current_appointments(access_token, patient_id, now)
        except OpenEmrRequestError:
            return NO_ACTION_RESPONSE, "unavailable"
        state.offered_appointments = tuple(offered)
        if not offered:
            return NO_APPOINTMENTS_RESPONSE, "listed"
        lines = "\n".join(
            f"- {_window(appointment.starts_at, appointment.ends_at)}" for appointment in offered
        )
        return f"Here are your upcoming appointments:\n{lines}", "listed"

    def _extract_document_fields(
        self, state: ConversationState, upload_id: str, now: datetime
    ) -> tuple[str, str]:
        """Read an upload this patient made and offer what it says, confirming nothing.

        The extracted values are shown as suggestions the patient still states
        themselves. Nothing here is a `ValidatedWrite` and nothing here can become one:
        a value only reaches the record when the model proposes it as a writing tool
        call, the validator vouches for it, and the patient confirms it (FR-23).
        """
        if self.services.ocr is None or upload_id != state.upload_id:
            # An upload id the model invented resolves to nothing, exactly as TICK-060
            # intends; there is no lookup here that would accept one.
            return UNKNOWN_UPLOAD_RESPONSE, "unavailable"
        identity = self.services.ocr.identity(upload_id, now)
        if identity is None:
            return UNKNOWN_UPLOAD_RESPONSE, "unavailable"
        read = _readable_identity(identity)
        if not read:
            return UPLOAD_UNREADABLE_RESPONSE, "read"
        return (
            f"Here is what I could read from your document. Nothing has been saved -- "
            f"please tell me if any of it is wrong:\n{read}"
        ), "read"

    # --- Writes ---------------------------------------------------------------------

    async def _execute_write(
        self,
        call: Any,
        state: ConversationState,
        handle: str,
        access_token: str | None,
        patient_id: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        """Validate, then either read the change back or -- if this turn agreed to the
        one already read back -- perform it.

        `validate_write` runs before anything is compared, so the comparison that decides
        "this is a confirmation" is between two *validated* results and never between two
        model proposals. A model that re-proposes the same change in different words
        therefore still confirms it, and one that proposes a different change does not.

        A *refused* proposal abandons whatever was pending, rather than leaving it to be
        agreed to later. Without that, a patient who corrects a pending change with a
        value the validator will not vouch for -- "no, the state is Nowhere" -- is told to
        re-send it while the superseded change is still waiting, and their next "yes"
        saves the value they had just corrected. Refusing the correction must not make the
        original more likely to be written.
        """
        context = WriteContext(
            now=now,
            offered_appointments=state.offered_appointments,
            offered_slots=state.offered_slots,
        )
        try:
            validated = validate_write(call.tool, call.arguments.model_dump(), context=context)
        except ToolSurfaceError:
            state.clear_pending()
            raise
        if not _confirms(state.pending, validated):
            state.pending = validated
            state.pending_prompt = confirmation_prompt(validated)
            return state.pending_prompt, "awaiting_confirmation"
        state.clear_pending()
        authority = write_authority(validated, patient_confirmed=True)
        # The values that reach the service are `executable_arguments()`'s, which for a
        # writing tool are the authority's -- the validator's output, never the model's.
        arguments = executable_arguments(call, authority=authority)
        return await self._perform(
            call.tool, arguments, state, handle, access_token, patient_id, now
        )

    async def _perform(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        state: ConversationState,
        handle: str,
        access_token: str | None,
        patient_id: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        if access_token is None:
            return NO_ACTION_RESPONSE, "unavailable"
        if tool == "update_address":
            return await self._write_address(arguments, access_token)
        if tool == "update_demographics":
            return await self._write_demographics(arguments, access_token)
        if tool == "record_assessment_answer":
            return await self._record_answer(arguments, handle, access_token, now)
        if tool == "book_appointment":
            return await self._book(arguments, state, access_token, now)
        return await self._cancel(arguments, state, access_token, patient_id, now)

    async def _write_address(
        self, arguments: Mapping[str, Any], access_token: str
    ) -> tuple[str, str]:
        if self.services.demographics is None:
            return NO_ACTION_RESPONSE, "unavailable"
        try:
            address = confirm_address(
                Address(
                    street1=str(arguments["street1"]),
                    city=str(arguments["city"]),
                    state=str(arguments["state"]),
                    zip_code=str(arguments["zip_code"]),
                    street2=arguments.get("street2"),
                )
            )
            await self.services.demographics.write_confirmed_address(access_token, address)
        except (IdentityNotConfirmedError, OpenEmrRequestError):
            return NO_ACTION_RESPONSE, "write_failed"
        return "Saved. Your mailing address is updated.", "written"

    async def _write_demographics(
        self, arguments: Mapping[str, Any], access_token: str
    ) -> tuple[str, str]:
        if self.services.demographics is None:
            return NO_ACTION_RESPONSE, "unavailable"
        try:
            fields = confirm_demographic_fields(
                given_name=arguments.get("given_name"),
                family_name=arguments.get("family_name"),
                date_of_birth=arguments.get("date_of_birth"),
            )
            await self.services.demographics.write_confirmed_fields(access_token, fields)
        except (IdentityNotConfirmedError, OpenEmrRequestError):
            return NO_ACTION_RESPONSE, "write_failed"
        return "Saved. Your details are updated.", "written"

    async def _record_answer(
        self, arguments: Mapping[str, Any], handle: str, access_token: str, now: datetime
    ) -> tuple[str, str]:
        """Checkpoint one intake answer, starting the draft if there is not one yet.

        The draft id is the onboarding position, and it stays where it already lives:
        `sessions.cursor`, which ARCHITECTURE.md Sec. 5 reserves for exactly this opaque
        non-patient value. So a patient who answers one question this turn and the next
        one an hour later resumes the same OpenEMR draft, with the answers themselves
        held by OpenEMR rather than reconstructed here.
        """
        if self.services.onboarding is None or self.cursors is None:
            return NO_ACTION_RESPONSE, "unavailable"
        field_name, answer = str(arguments["field"]), str(arguments["answer"])
        try:
            cursor = await self._cursor(handle, access_token, now)
            await self.services.onboarding.checkpoint_field(
                access_token, cursor, field_name, _draft_value(field_name, answer), now
            )
        except (FieldCheckpointRejected, OpenEmrRequestError):
            return NO_ACTION_RESPONSE, "write_failed"
        return "Saved. I've recorded that answer.", "written"

    async def _cursor(self, handle: str, access_token: str, now: datetime) -> OnboardingCursor:
        assert self.services.onboarding is not None and self.cursors is not None
        raw = await asyncio.to_thread(self.cursors.load_cursor, handle, now)
        if raw:
            return OnboardingCursor.deserialize(raw)
        cursor = await self.services.onboarding.start(access_token)
        await asyncio.to_thread(self.cursors.save_cursor, handle, cursor.serialize(), now)
        return cursor

    async def _book(
        self,
        arguments: Mapping[str, Any],
        state: ConversationState,
        access_token: str,
        now: datetime,
    ) -> tuple[str, str]:
        if self.services.booking is None or self.services.appointment_request is None:
            return NO_ACTION_RESPONSE, "unavailable"
        try:
            booked = await self.services.booking.book(
                access_token,
                str(arguments["slot_token"]),
                self.services.appointment_request,
                now,
            )
        except SlotBookingError:
            state.offered_slots = ()
            return (
                "That appointment time is no longer available, so nothing was booked. "
                "Please ask me for open times again and choose a new one."
            ), "write_failed"
        except OpenEmrRequestError:
            return (
                "OpenEMR could not confirm that booking just now, so no appointment was "
                "created. Please try again."
            ), "write_failed"
        # The token is spent, so the model must not be shown it again next turn.
        state.offered_slots = ()
        return (f"Booked and confirmed: {_window(booked.starts_at, booked.ends_at)}."), "written"

    async def _cancel(
        self,
        arguments: Mapping[str, Any],
        state: ConversationState,
        access_token: str,
        patient_id: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        if self.services.cancellation is None or patient_id is None:
            return NO_ACTION_RESPONSE, "unavailable"
        try:
            await self.services.cancellation.cancel(
                access_token, patient_id, str(arguments["appointment_token"]), now
            )
        except StaleAppointmentTokenError:
            state.offered_appointments = ()
            return (
                "That appointment reference is no longer valid, so nothing was "
                "cancelled. Please ask to see your appointments again and tell me which "
                "one you mean."
            ), "write_failed"
        except AppointmentAlreadyCancelledError:
            state.offered_appointments = ()
            return "That appointment has already been cancelled.", "write_failed"
        except (AppointmentNotFoundError, OpenEmrRequestError):
            return (
                "OpenEMR could not confirm that cancellation just now, so the "
                "appointment was not cancelled. Please try again."
            ), "write_failed"
        state.offered_appointments = ()
        return "Cancelled and confirmed. That appointment is no longer booked.", "written"


# The writing tools this module executes. Identical to
# `ai_server.llm.validation.VALIDATED_WRITING_TOOLS` and asserted equal by test rather
# than imported, so adding a writing tool without a branch in `_perform` fails loudly.
_WRITING_TOOLS_HANDLED_HERE = frozenset(
    {
        "update_address",
        "update_demographics",
        "record_assessment_answer",
        "book_appointment",
        "cancel_appointment",
    }
)


def _confirms(pending: ValidatedWrite | None, validated: ValidatedWrite) -> bool:
    """Whether this turn agrees to the change already read back to the patient.

    Compares two `ValidatedWrite`s, which is why no phrase list is needed: agreement is
    the model re-proposing the same change, and the validator -- not this comparison --
    decides what "the same" means, because it normalises both sides. A correction lands
    here as a different `values` mapping and is therefore not a confirmation.
    """
    if pending is None or pending.tool != validated.tool:
        return False
    return dict(pending.values) == dict(validated.values)


def _draft_value(field_name: str, answer: str) -> object:
    """Reshape one canonical answer into what `OnboardingFlow.checkpoint_field` takes.

    TICK-061's canonicalisers produce a flat string per field (`"video,weekday_morning"`,
    `"email a@b.c"`) because that is the shape the model was asked for and the shape a
    confirmation reads back. `fields.validate_field` takes the structured form. This is
    the one place the two meet, and it only ever re-splits a value the validator already
    vouched for -- it never parses anything the patient typed.
    """
    if field_name == "preferred_contact":
        method, _, value = answer.partition(" ")
        return {"method": method} if not value else {"method": method, "value": value}
    if field_name == "visit_preference":
        visit_format, _, time_window = answer.partition(",")
        return {"format": visit_format, "time_window": time_window}
    if field_name == "accommodations":
        return {"selected": [] if answer == "none" else answer.split(",")}
    return answer


def _consent_given(message: str | None) -> bool:
    """Read explicit upload consent out of the chat page's own action envelope.

    Not a phrasing rule and not an intent detector (AC1): the page's JavaScript sends a
    fixed JSON control object whose `consent` field is its consent checkbox, and this
    decodes that one boolean. A patient typing the words "I consent" produces no such
    object and grants nothing, which is the point -- FR-21 wants the tick, not the
    sentiment.
    """
    if not message:
        return False
    try:
        parsed = json.loads(message)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("consent") is True


def _readable_identity(identity: ExtractedIdentity) -> str:
    lines = [
        f"{label}: {value}"
        for label, value in (
            ("Name", identity.name),
            ("Date of birth", identity.date_of_birth),
            ("Address", identity.address),
        )
        if value
    ]
    return "\n".join(lines)


def _window(starts_at: datetime, ends_at: datetime) -> str:
    return (
        f"{starts_at:%A} {starts_at.day} {starts_at:%B %Y}, "
        f"{_clock(starts_at)} to {_clock(ends_at)}"
    )


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def _chunks(text: str) -> list[str]:
    """Split a reply into stream-sized pieces at whitespace, keeping every character.

    Concatenating the result reproduces `text` exactly, including its newlines: the
    confirmation prompts and appointment lists are line-structured and the chat page
    renders them with `white-space: pre-wrap`, so a split that dropped or normalised a
    character would change what the patient reads.
    """
    if not text:
        return [""]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _STREAM_CHUNK_CHARACTERS, len(text))
        if end < len(text):
            # Prefer the last space inside the window so a word is not torn in half;
            # a window with no space at all is emitted as-is rather than grown.
            space = text.rfind(" ", start + 1, end + 1)
            if space > start:
                end = space + 1
        pieces.append(text[start:end])
        start = end
    return pieces


def unavailable_model_turn_service() -> ModelTurnService:
    """Return a service that always reports the fixed unavailable message (D12)."""
    return ModelTurnService(client=None)
