"""The patient-facing chat page and the request shape behind it.

The page never talks to OpenEMR directly (FR-4): its only fetch target is this
server's own `/api/chat` route, sent with the AI-session cookie. Chunks stream to the
browser as they arrive from `ModelTurnService` (TICK-063); AI-server or model-server
unavailability surfaces the same fallback text in both the streamed reply and a
client-side panel that will render even if the request never completes (FR-19).

`ChatService` used to live here too: it built a Groq planning payload out of the
patient's typed message and `SchedulingContext`, and `GroqWorkflow` ran the plan
through `BookingTool`. TICK-063 took it off every request path and TICK-064 deleted it,
because D13 moves scheduling planning to the local model and D3 forbids the patient's
words being what leaves. What is left here is the page, the request model, and the two
pieces `_build_model_turn_service` still uses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ai_server.ocr.service import MAX_UPLOAD_BYTES
from ai_server.openemr.adapter import OpenEmrConfigurationError
from ai_server.scheduling.booking import AppointmentRequest
from ai_server.scheduling.slots import CandidateSlot

_MAX_IMAGE_BASE64_LENGTH = ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 1_000


class ChatTurnRequest(BaseModel):
    """The only shape the iframe may send to the AI server for a turn."""

    message: str = Field(min_length=1, max_length=4_000)
    image_base64: str | None = Field(default=None, max_length=_MAX_IMAGE_BASE64_LENGTH)


# The model server is not configured or not reachable, so the assistant cannot handle
# *any* request this deployment receives -- a deployment fault rather than a per-turn
# one, and deliberately worded as its own string (TICK-048). Since TICK-063 this is the
# chat's whole-outage message (D12); an unreachable *Groq* is not this, because it costs
# only general-knowledge answers and `ModelTurnService` has its own string for that. It
# reports that the assistant is unavailable rather than that scheduling failed, since
# the turn may have been about anything.
#
# Every clause is constrained by TICK-065 AC3, which is why this reads the way it does:
#
# - "temporarily unavailable", because an outage that sounds permanent, or that sounds
#   like a broken feature, is the failure D12 accepted a hard model dependency to avoid.
# - It says the portal still works, in as many words. With no fallback path this is the
#   only thing the patient can still do, so leaving them to infer it is not good enough.
# - It names nothing internal. Not the model server, not the provider, not this service.
#   "Your patient portal" is the thing the patient is looking at; a component name would
#   tell them something they cannot act on.
# - The one next step it offers is the portal's *own* scheduling screen, which FR-19
#   requires and which is not a degraded path through this system -- no write reaches
#   OpenEMR through anything that is currently down. It is named as the patient's own
#   portal menu (the same place the client-side fallback panel below points at), never
#   the staff-only native scheduling screen a portal user cannot open.
ASSISTANT_UNAVAILABLE_RESPONSE = (
    "The AI assistant is temporarily unavailable, so it could not handle your request. "
    "Your patient portal is still working normally. Please try again in a little while, "
    "or contact the clinic directly. To book or change an appointment in the meantime, "
    "use the appointment scheduling option in your OpenEMR portal menu."
)


class NoMappedCandidateSource:
    """Honestly reports zero open-slot candidates: no OpenEMR endpoint exists on the
    pinned v8.3.0 release for provider availability, regular office hours, or
    closures (`evidence/TICK-001/ENDPOINT_MATRIX.md`, "Implementation-blocking API
    gap" on all three), and ADR-3 (`ARCHITECTURE.md`) forbids a database workaround or
    an invented default in their place -- the same discipline
    `ai_server.openemr.adapter.OpenEmrScheduleAdapter.availability/office_hours/
    closures` already document for this identical gap.

    Using this keeps `SlotDiscoveryService` genuinely wired into the `find_slots` tool
    instead of a value hardcoded there (TICK-034 AC2): the offered slots are empty today
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
  /* A reply is inserted with textContent, so HTML would otherwise collapse its line
     breaks into spaces. The address review (TICK-050) puts each component of a parsed
     address on its own labelled line, and that structure has to survive to the screen
     to be readable at all (NFR-19). pre-wrap preserves the newlines while still
     wrapping long lines normally; no existing single-line reply changes appearance. */
  #chat-transcript li .message-body { white-space: pre-wrap; }
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
