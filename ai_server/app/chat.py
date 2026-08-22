"""The patient-facing chat page and the turn-streaming boundary behind it.

The page never talks to OpenEMR directly (FR-4): its only fetch target is this
server's own `/api/chat` route, sent with the AI-session cookie. Chunks stream to the
browser as they arrive from `GroqWorkflow` (TICK-010); AI-server or LLM
unavailability surfaces the same fallback text in both the streamed reply and a
client-side panel that will render even if the request never completes (FR-19).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from pydantic import BaseModel, Field

from ai_server.llm.groq import (
    AuthoritativeTool,
    AuthoritativeToolResult,
    GroqWorkflow,
    PlanningOutput,
)
from ai_server.ocr.service import MAX_UPLOAD_BYTES
from ai_server.openemr.adapter import OpenEmrConfigurationError, OpenEmrRequestError
from ai_server.privacy.gate import (
    AnonymousAppointment,
    AnonymousSlot,
    OutboundMessage,
    OutboundPayload,
    ResponseFormat,
    SchedulingContext,
    SchedulingRules,
)
from ai_server.scheduling.appointments import AppointmentDiscoveryService
from ai_server.scheduling.booking import AppointmentRequest, BookingService, SlotBookingError
from ai_server.scheduling.cancel import (
    AppointmentAlreadyCancelledError,
    AppointmentNotFoundError,
    CancellationService,
    StaleAppointmentTokenError,
)
from ai_server.scheduling.reschedule import RescheduleCancellationFailedError, RescheduleService
from ai_server.scheduling.slots import CandidateSlot, SlotDiscoveryService

SYSTEM_PROMPT = (
    "You help a patient discuss scheduling for this clinic. Use only the office hours, "
    "closures, open slots, and current appointments given in scheduling_context, and "
    "only the actions allowed by scheduling_rules -- never state or imply that an "
    "appointment was booked, rescheduled, or cancelled; that can only be confirmed by "
    "OpenEMR, not by you. To cancel, reference one of the caller's own current "
    "appointments by its appointment_token; never invent or ask the patient for an "
    "appointment id. To reschedule, reference one of the caller's own current "
    "appointments by its appointment_token together with one open slot by its "
    "slot_token; never invent or ask the patient for either identifier. If every "
    "scheduling action is disabled, say so and do not propose one. Do not give "
    "clinical or treatment advice; if asked, say you can only help with scheduling."
)

# Cancellation (TICK-036) and reschedule (TICK-020, a server-side composition of
# booking + cancellation, never a new OpenEMR write path) are both wired to real
# `AuthoritativeTool` behavior, so both are enabled here; the model still only learns
# actual appointments/slots to reference from scheduling_context's
# current_appointments/open_slots, never from anything client- or model-supplied.
_SCHEDULING_RULES = SchedulingRules(
    minimum_booking_notice_minutes=1440,
    booking_enabled=False,
    rescheduling_enabled=True,
    cancellation_enabled=True,
)
_TIMEZONE = "America/Chicago"


# An `upload_identity_document` onboarding action (TICK-044,
# `ai_server/app/onboarding_chat.py`) carries its base64-encoded image in this
# dedicated field, not `message` -- keeping `message` at its original 4,000-character
# cap for every other chat turn (scheduling included) instead of widening it for
# everyone just to fit an image. Base64 inflates raw bytes by 4/3 (ceiling to a whole
# group of 4 characters); a fixed pad covers the field's own JSON encoding overhead,
# so a max-size OCR upload (`ai_server.ocr.service`'s own `MAX_UPLOAD_BYTES`) is never
# rejected here before `OcrService` itself validates it.
_MAX_IMAGE_BASE64_LENGTH = ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 1_000


class ChatTurnRequest(BaseModel):
    """The only shape the iframe may send to the AI server for a turn."""

    message: str = Field(min_length=1, max_length=4_000)
    image_base64: str | None = Field(default=None, max_length=_MAX_IMAGE_BASE64_LENGTH)


NO_ACTION_SUMMARY = "No scheduling action is available yet in this demo."

# Groq is not configured at all, so the assistant cannot handle *any* request this
# deployment receives -- a deployment fault, distinct from `PLANNING_FAILED_RESPONSE`'s
# per-turn planning fault, and deliberately worded as its own string (TICK-048). It
# reports that the assistant is unavailable rather than that scheduling failed, since
# the turn may have been about anything. FR-19 still wants a route to OpenEMR's own
# scheduling UI from this path, so one is offered -- but only for the appointment case
# it actually covers, and named as the patient's own portal menu (the same place the
# client-side fallback panel below points at), never the staff-only native scheduling
# screen a portal user cannot open.
ASSISTANT_UNAVAILABLE_RESPONSE = (
    "The AI assistant is not available right now, so it could not handle your request. "
    "Please try again later, or contact the clinic directly. To book or change an "
    "appointment in the meantime, use the appointment scheduling option in your "
    "OpenEMR portal menu."
)


class NoActionTool(AuthoritativeTool):
    """The fallback `AuthoritativeTool` for every intent `BookingTool` cannot perform.

    `BookingTool` delegates here for a `book` intent missing a `slot_token`, a
    `cancel` intent missing an `appointment_token`, a `reschedule` intent missing
    either token, or any intent missing this turn's delegated session credentials,
    and `_build_scheduling_tool` (`ai_server/app/main.py`) uses this as the whole
    tool for any environment missing the OpenEMR settings booking, cancellation, or
    reschedule need (TICK-034 AC3/AC5, TICK-036, TICK-020).
    """

    async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
        del plan  # No authoritative action exists for any of these planned intents.
        return AuthoritativeToolResult(public_summary=NO_ACTION_SUMMARY)


@dataclass(frozen=True)
class BookingTool(AuthoritativeTool):
    """Executes a `book` plan through `BookingService`, a `cancel` plan through
    `CancellationService`, and a `reschedule` plan through `RescheduleService`
    (which itself composes those same two services, TICK-020); everything else
    falls back to `NoActionTool` (TICK-034 AC3, TICK-036 AC4, TICK-020 AC1).

    `access_token`/`patient_id` are this turn's already-delegated OpenEMR credentials
    (`SessionStore.access_token`/`patient_uuid`, `ai_server/app/main.py`'s `/api/chat`
    handler) -- baked into a fresh instance per turn by `_build_scheduling_tool`'s
    factory, never stored anywhere beyond that. `patient_id` carries the session's
    OpenEMR patient UUID: `CancellationService.cancel`'s `patient_id` argument uses it
    as the binding identity `AnonymousAppointmentStore` issued the caller's
    `appointment_token`s under. `BookingService.book` no longer takes a patient id at
    all (TICK-040) -- the module-added Portal route it calls resolves the caller's
    numeric OpenEMR patient id itself, server-side, from the bearer token.
    """

    booking: BookingService
    cancellation: CancellationService
    reschedule: RescheduleService
    appointment_request: AppointmentRequest
    access_token: str | None
    patient_id: str | None
    now: datetime

    async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
        if plan.intent == "book" and plan.slot_token is not None:
            return await self._execute_book(plan.slot_token)
        if plan.intent == "cancel" and plan.appointment_token is not None:
            return await self._execute_cancel(plan.appointment_token)
        if (
            plan.intent == "reschedule"
            and plan.slot_token is not None
            and plan.appointment_token is not None
        ):
            return await self._execute_reschedule(plan.appointment_token, plan.slot_token)
        return await NoActionTool().execute(plan)

    async def _execute_book(self, slot_token: str) -> AuthoritativeToolResult:
        if self.access_token is None:
            # No delegated access token for this turn -- no OpenEMR call is possible.
            # Unlike cancel/reschedule, booking (TICK-040) no longer needs patient_id
            # at all -- the module route resolves the caller's patient id itself,
            # server-side, from the access token -- so a session missing only
            # patient_id (SessionStore.patient_uuid() is documented best-effort,
            # TICK-028) must not be refused booking on that account alone.
            return AuthoritativeToolResult(public_summary=NO_ACTION_SUMMARY)
        try:
            booked = await self.booking.book(
                self.access_token,
                slot_token,
                self.appointment_request,
                self.now,
            )
        except SlotBookingError:
            return AuthoritativeToolResult(
                public_summary=(
                    "That appointment time is no longer available. Please ask for "
                    "open times again and choose a new one."
                )
            )
        except OpenEmrRequestError:
            return AuthoritativeToolResult(
                public_summary=(
                    "OpenEMR could not confirm that booking just now, so no "
                    "appointment was created. Please try again."
                )
            )
        return AuthoritativeToolResult(
            public_summary=(
                "Booked and confirmed by OpenEMR: appointment "
                f"{booked.id}, {booked.starts_at.isoformat()} to {booked.ends_at.isoformat()}."
            )
        )

    async def _execute_cancel(self, appointment_token: str) -> AuthoritativeToolResult:
        if self.access_token is None or self.patient_id is None:
            # No delegated session credentials for this turn -- no OpenEMR call is
            # possible, same as booking above.
            return AuthoritativeToolResult(public_summary=NO_ACTION_SUMMARY)
        try:
            cancelled = await self.cancellation.cancel(
                self.access_token, self.patient_id, appointment_token, self.now
            )
        except StaleAppointmentTokenError:
            return AuthoritativeToolResult(
                public_summary=(
                    "That appointment reference is no longer valid. Please ask to see "
                    "your current appointments again and choose one to cancel."
                )
            )
        except AppointmentAlreadyCancelledError:
            return AuthoritativeToolResult(
                public_summary="That appointment has already been cancelled."
            )
        except (AppointmentNotFoundError, OpenEmrRequestError):
            return AuthoritativeToolResult(
                public_summary=(
                    "OpenEMR could not confirm that cancellation just now, so the "
                    "appointment was not cancelled. Please try again."
                )
            )
        return AuthoritativeToolResult(
            public_summary=(
                "Cancelled and confirmed by OpenEMR: appointment "
                f"{cancelled.id} is now {cancelled.status}."
            )
        )

    async def _execute_reschedule(
        self, appointment_token: str, slot_token: str
    ) -> AuthoritativeToolResult:
        if self.access_token is None or self.patient_id is None:
            # No delegated session credentials for this turn -- no OpenEMR call is
            # possible, same as booking/cancellation above.
            return AuthoritativeToolResult(public_summary=NO_ACTION_SUMMARY)
        try:
            rescheduled = await self.reschedule.reschedule(
                self.access_token,
                self.patient_id,
                slot_token,
                appointment_token,
                self.appointment_request,
                self.now,
            )
        except SlotBookingError:
            return AuthoritativeToolResult(
                public_summary=(
                    "That new appointment time is no longer available, so nothing "
                    "was rescheduled and your original appointment is unchanged. "
                    "Please ask for open times again and choose a new one."
                )
            )
        except RescheduleCancellationFailedError as exc:
            return AuthoritativeToolResult(
                public_summary=(
                    "Your new appointment is confirmed by OpenEMR: appointment "
                    f"{exc.booked.id}, {exc.booked.starts_at.isoformat()} to "
                    f"{exc.booked.ends_at.isoformat()}. However, OpenEMR could not "
                    "confirm that your original appointment was cancelled, so it "
                    "may still be active -- please contact the clinic if it isn't "
                    "cancelled."
                )
            )
        except OpenEmrRequestError:
            return AuthoritativeToolResult(
                public_summary=(
                    "OpenEMR could not confirm that reschedule just now, so nothing "
                    "was booked and your original appointment is unchanged. Please "
                    "try again."
                )
            )
        return AuthoritativeToolResult(
            public_summary=(
                "Rescheduled and confirmed by OpenEMR: your new appointment is "
                f"{rescheduled.booked.id}, {rescheduled.booked.starts_at.isoformat()} "
                f"to {rescheduled.booked.ends_at.isoformat()}, and your original "
                f"appointment {rescheduled.cancelled.id} is now "
                f"{rescheduled.cancelled.status}."
            )
        )


class NoMappedCandidateSource:
    """Honestly reports zero open-slot candidates: no OpenEMR endpoint exists on the
    pinned v8.3.0 release for provider availability, regular office hours, or
    closures (`evidence/TICK-001/ENDPOINT_MATRIX.md`, "Implementation-blocking API
    gap" on all three), and ADR-3 (`ARCHITECTURE.md`) forbids a database workaround or
    an invented default in their place -- the same discipline
    `ai_server.openemr.adapter.OpenEmrScheduleAdapter.availability/office_hours/
    closures` already document for this identical gap.

    Using this keeps `SlotDiscoveryService` genuinely wired into `ChatService._payload`
    instead of a value hardcoded there (TICK-034 AC2): `open_slots` is empty today
    because no source has real candidates to report, not because the call was never
    made. A future ticket that maps a real candidate-source endpoint only has to
    replace this one implementation.
    """

    async def candidate_slots(self) -> list[CandidateSlot]:
        return []


@dataclass(frozen=True)
class BookingToolSettings:
    """Validated, admin-configured appointment fields this demo's single office uses
    for every AI-booked appointment.

    `ai_server.scheduling.booking.AppointmentRequest`'s own docstring is explicit that
    this module has no office configuration of its own and the caller must supply
    these fields; this is that caller's configuration boundary, kept out of
    `booking.py` itself (TICK-034's Out of Scope: "Changing `BookingService` ...
    themselves").
    """

    category_id: str
    title: str
    facility_id: str
    billing_location_id: str
    provider_id: str | None = None

    @classmethod
    def from_environment(cls) -> BookingToolSettings:
        """Parse the required local-office booking fields once during startup."""
        values = {
            "AI_BOOKING_CATEGORY_ID": os.environ.get("AI_BOOKING_CATEGORY_ID"),
            "AI_BOOKING_TITLE": os.environ.get("AI_BOOKING_TITLE"),
            "AI_BOOKING_FACILITY_ID": os.environ.get("AI_BOOKING_FACILITY_ID"),
            "AI_BOOKING_BILLING_LOCATION_ID": os.environ.get("AI_BOOKING_BILLING_LOCATION_ID"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise OpenEmrConfigurationError(
                f"missing required booking settings: {', '.join(missing)}"
            )
        return cls(
            category_id=str(values["AI_BOOKING_CATEGORY_ID"]),
            title=str(values["AI_BOOKING_TITLE"]),
            facility_id=str(values["AI_BOOKING_FACILITY_ID"]),
            billing_location_id=str(values["AI_BOOKING_BILLING_LOCATION_ID"]),
            provider_id=os.environ.get("AI_BOOKING_PROVIDER_ID") or None,
        )

    def appointment_request(self) -> AppointmentRequest:
        return AppointmentRequest(
            category_id=self.category_id,
            title=self.title,
            facility_id=self.facility_id,
            billing_location_id=self.billing_location_id,
            provider_id=self.provider_id,
        )


ToolFactory = Callable[[str | None, str | None, datetime], AuthoritativeTool]


def no_action_tool_factory() -> ToolFactory:
    """The fallback factory: every turn gets a fresh, stateless `NoActionTool`."""
    return lambda access_token, patient_id, now: NoActionTool()


@dataclass(frozen=True)
class ChatService:
    """Builds the approved payload for a turn and streams the workflow's reply."""

    workflow: GroqWorkflow | None
    tool_factory: ToolFactory
    slot_discovery: SlotDiscoveryService | None = None
    appointment_discovery: AppointmentDiscoveryService | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def stream_reply(
        self, message: str, access_token: str | None = None, patient_id: str | None = None
    ) -> AsyncIterator[str]:
        """Yield the assistant-unavailable message, or the workflow's streamed reply.

        `access_token`/`patient_id` are this turn's delegated OpenEMR credentials
        (TICK-034 AC1); they are used only to build this turn's payload and tool, and
        are held nowhere once this call returns.
        """
        if self.workflow is None:
            yield ASSISTANT_UNAVAILABLE_RESPONSE
            return
        now = self.clock()
        payload = await self._payload(message, access_token, patient_id, now)
        tool = self.tool_factory(access_token, patient_id, now)
        async for chunk in self.workflow.respond(payload, tool):
            yield chunk

    async def _payload(
        self, message: str, access_token: str | None, patient_id: str | None, now: datetime
    ) -> OutboundPayload:
        open_slots: list[AnonymousSlot] = []
        if self.slot_discovery is not None and access_token is not None:
            tokens = await self.slot_discovery.open_slots(access_token, now)
            open_slots = [
                AnonymousSlot(slot_token=t.slot_token, starts_at=t.starts_at, ends_at=t.ends_at)
                for t in tokens
            ]
        current_appointments: list[AnonymousAppointment] = []
        if (
            self.appointment_discovery is not None
            and access_token is not None
            and patient_id is not None
        ):
            appointment_tokens = await self.appointment_discovery.current_appointments(
                access_token, patient_id, now
            )
            current_appointments = [
                AnonymousAppointment(
                    appointment_token=t.appointment_token, starts_at=t.starts_at, ends_at=t.ends_at
                )
                for t in appointment_tokens
            ]
        return OutboundPayload(
            model="openai/gpt-oss-120b",
            messages=[
                OutboundMessage(role="system", content=SYSTEM_PROMPT),
                OutboundMessage(role="user", content=message),
            ],
            scheduling_context=SchedulingContext(
                current_datetime=now,
                timezone=_TIMEZONE,
                office_hours=[],
                closures=[],
                open_slots=open_slots,
                current_appointments=current_appointments,
            ),
            scheduling_rules=_SCHEDULING_RULES,
            response_format=ResponseFormat(type="json_schema", schema_version="1"),
        )


def unavailable_chat_service() -> ChatService:
    """Return a service that always reports the fixed unavailable message."""
    return ChatService(workflow=None, tool_factory=no_action_tool_factory())


# The embedded chat page. It is intentionally a single static document: FastAPI is
# the only backend (ARCHITECTURE.md "Chat UI" component), there is no bundler or
# frontend framework in this project's stack, and the page has no server-rendered
# patient data to template (it learns everything it shows from `/api/chat` itself).
CHAT_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Chat</title>
<style>
  :root {
    color-scheme: light;
    /* #1a1a1a on #ffffff and #ffffff on #0b5fff both clear WCAG AA (4.5:1) for
       normal text; see ai_server/tests/test_chat.py for the checked ratios. */
    --text: #1a1a1a;
    --bg: #ffffff;
    --accent: #0b5fff;
    --border: #4a4a4a;
    --error-bg: #fdecea;
    --error-text: #7a1f1a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, sans-serif;
    color: var(--text);
    background: var(--bg);
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }
  main {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 1rem;
    gap: 0.75rem;
    max-width: 40rem;
    width: 100%;
    margin: 0 auto;
  }
  h1 { font-size: 1.1rem; margin: 0; }
  #chat-status {
    font-size: 0.9rem;
    font-weight: 600;
  }
  #chat-transcript {
    flex: 1;
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    overflow-y: auto;
    min-height: 12rem;
  }
  #chat-transcript li {
    border: 1px solid var(--border);
    border-radius: 0.4rem;
    padding: 0.5rem 0.75rem;
  }
  #chat-transcript li[data-role="user"] { align-self: flex-end; }
  #chat-transcript li .role-label { display: block; font-size: 0.75rem; font-weight: 600; }
  #chat-fallback {
    display: none;
    border: 2px solid var(--error-text);
    background: var(--error-bg);
    color: var(--error-text);
    border-radius: 0.4rem;
    padding: 0.75rem;
  }
  #chat-fallback[data-visible="true"] { display: block; }
  form { display: flex; flex-direction: column; gap: 0.35rem; }
  label { font-weight: 600; }
  textarea {
    font: inherit;
    padding: 0.5rem;
    border: 1px solid var(--border);
    border-radius: 0.35rem;
    resize: vertical;
    min-height: 3.5rem;
  }
  button {
    font: inherit;
    align-self: flex-start;
    padding: 0.5rem 1.25rem;
    border: 1px solid var(--accent);
    border-radius: 0.35rem;
    background: var(--accent);
    color: #ffffff;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  #upload-identity {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    border: 1px solid var(--border);
    border-radius: 0.35rem;
    padding: 0.5rem 0.75rem;
  }
  #upload-caption { margin: 0; font-size: 0.85rem; }
  a { color: var(--accent); }
  :focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 2px;
  }
</style>
</head>
<body>
<main>
  <h1>AI Chat</h1>
  <p id="chat-status" role="status" aria-live="polite">Status: Ready.</p>
  <ul id="chat-transcript" role="log" aria-live="polite" aria-label="Conversation"></ul>
  <div id="chat-fallback" role="alert" data-visible="false">
    <strong>Chat unavailable.</strong>
    <p>
      The AI assistant cannot respond right now. Please close this chat panel and use
      the appointment scheduling option in your OpenEMR portal menu instead.
    </p>
  </div>
  <form id="chat-form">
    <label for="chat-input">Message</label>
    <textarea id="chat-input" name="message" required maxlength="4000"
      aria-describedby="chat-status"></textarea>
    <button type="submit" id="chat-send">Send</button>
  </form>
  <div id="upload-identity">
    <p id="upload-caption">
      During onboarding, you can attach a photo of your ID to prefill your name, date
      of birth, and address as suggestions you still confirm or correct yourself. A
      synthetic ID image is read locally and discarded once you confirm or correct
      those fields.
    </p>
    <label for="upload-consent">
      <input type="checkbox" id="upload-consent">
      I consent to a synthetic ID image being read locally for this purpose.
    </label>
    <input type="file" id="id-photo-input" accept="image/png,image/jpeg" disabled
      aria-describedby="upload-caption">
  </div>
</main>
<script>
(function () {
  "use strict";
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var sendButton = document.getElementById("chat-send");
  var idPhotoInput = document.getElementById("id-photo-input");
  var uploadConsent = document.getElementById("upload-consent");
  var status = document.getElementById("chat-status");
  var transcript = document.getElementById("chat-transcript");
  var fallback = document.getElementById("chat-fallback");

  function setStatus(text) {
    status.textContent = "Status: " + text;
  }

  function showFallback() {
    fallback.setAttribute("data-visible", "true");
    setStatus("Unavailable.");
  }

  function appendMessage(role, text) {
    var item = document.createElement("li");
    item.setAttribute("data-role", role);
    var label = document.createElement("span");
    label.className = "role-label";
    label.textContent = role === "user" ? "You" : "Assistant";
    var body = document.createElement("span");
    body.className = "message-body";
    body.textContent = text;
    item.appendChild(label);
    item.appendChild(body);
    transcript.appendChild(item);
    return body;
  }

  // Shared by the typed-message form and the ID-photo attachment below: both send
  // their JSON-shaped body through this one call to the same /api/chat turn below
  // (TICK-044 reuses the existing chat-message pipe instead of a separate route).
  // `imageBase64` travels as its own request field, never appended into `message`,
  // so the 4,000-character message cap stays the same for every other chat turn.
  function sendMessage(message, displayText, imageBase64) {
    sendButton.disabled = true;
    idPhotoInput.disabled = true;
    fallback.setAttribute("data-visible", "false");
    appendMessage("user", displayText);
    var replyBody = appendMessage("assistant", "");
    setStatus("Sending...");

    var controller = new AbortController();
    var stallTimer = setTimeout(function () { controller.abort(); }, 30000);

    function resetStallTimer() {
      clearTimeout(stallTimer);
      stallTimer = setTimeout(function () { controller.abort(); }, 30000);
    }

    function reenableControls() {
      sendButton.disabled = false;
      idPhotoInput.disabled = !uploadConsent.checked;
    }

    var body = { message: message };
    if (imageBase64) {
      body.image_base64 = imageBase64;
    }

    fetch("/api/chat", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal
    }).then(function (response) {
      if (!response.ok || !response.body) {
        throw new Error("chat request failed");
      }
      setStatus("Receiving response...");
      var reader = response.body.getReader();
      var decoder = new TextDecoder();

      function read() {
        return reader.read().then(function (result) {
          if (result.done) {
            clearTimeout(stallTimer);
            setStatus("Response complete.");
            reenableControls();
            return;
          }
          resetStallTimer();
          replyBody.textContent += decoder.decode(result.value, { stream: true });
          return read();
        });
      }
      return read();
    }).catch(function () {
      clearTimeout(stallTimer);
      showFallback();
      reenableControls();
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var message = input.value.trim();
    if (!message) {
      return;
    }
    input.value = "";
    sendMessage(message, message);
  });

  // The file input stays disabled until this checkbox is explicitly ticked (FR-21,
  // ONBOARDING_CONTRACT.md field 1: consent is its own unticked-by-default step, not
  // implied by selecting a file) -- nothing is ever read from disk before that.
  uploadConsent.addEventListener("change", function () {
    idPhotoInput.disabled = !uploadConsent.checked;
  });

  idPhotoInput.addEventListener("change", function () {
    var file = idPhotoInput.files && idPhotoInput.files[0];
    if (!file || !uploadConsent.checked) {
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var dataUrl = String(reader.result || "");
      var commaIndex = dataUrl.indexOf(",");
      var base64 = commaIndex >= 0 ? dataUrl.slice(commaIndex + 1) : dataUrl;
      var message = JSON.stringify({
        action: "upload_identity_document",
        consent: uploadConsent.checked
      });
      sendMessage(message, "Attached ID photo: " + file.name, base64);
      idPhotoInput.value = "";
      uploadConsent.checked = false;
      idPhotoInput.disabled = true;
    };
    reader.readAsDataURL(file);
  });

  input.focus();
})();
</script>
</body>
</html>
"""
