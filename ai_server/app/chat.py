"""The patient-facing chat page and the turn-streaming boundary behind it.

The page never talks to OpenEMR directly (FR-4): its only fetch target is this
server's own `/api/chat` route, sent with the AI-session cookie. Chunks stream to the
browser as they arrive from `GroqWorkflow` (TICK-010); AI-server or LLM
unavailability surfaces the same fallback text in both the streamed reply and a
client-side panel that will render even if the request never completes (FR-19).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from pydantic import BaseModel, Field

from ai_server.llm.groq import (
    UNAVAILABLE_RESPONSE,
    AuthoritativeTool,
    AuthoritativeToolResult,
    GroqWorkflow,
    PlanningOutput,
)
from ai_server.privacy.gate import OutboundMessage, OutboundPayload

SYSTEM_PROMPT = "Scheduling assistant instructions"

# No authoritative scheduling tool is wired yet (booking/rescheduling/cancellation
# live behind separate, still-blocked tickets), so every request is sent with every
# scheduling action disabled. This keeps the model from ever describing a booking,
# reschedule, or cancellation as real (FR-20) while the underlying capability doesn't
# exist yet.
_SCHEDULING_RULES = {
    "minimum_booking_notice_minutes": 1440,
    "booking_enabled": False,
    "rescheduling_enabled": False,
    "cancellation_enabled": False,
}
_TIMEZONE = "America/Chicago"


class ChatTurnRequest(BaseModel):
    """The only shape the iframe may send to the AI server for a turn."""

    message: str = Field(min_length=1, max_length=4_000)


class NoActionTool(AuthoritativeTool):
    """The only `AuthoritativeTool` available until a scheduling tool ships.

    Booking, rescheduling, and cancellation are unimplemented (TICK-020 is blocked)
    and guided onboarding's draft/completion checkpoint is unimplemented (TICK-017 is
    blocked), so this tool performs no OpenEMR operation and reports none occurred.
    """

    async def execute(self, plan: PlanningOutput) -> AuthoritativeToolResult:
        del plan  # No authoritative action exists yet for any planned intent.
        return AuthoritativeToolResult(
            public_summary="No scheduling action is available yet in this demo."
        )


@dataclass(frozen=True)
class ChatService:
    """Builds the approved payload for a turn and streams the workflow's reply."""

    workflow: GroqWorkflow | None
    tool: AuthoritativeTool
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def stream_reply(self, message: str) -> AsyncIterator[str]:
        """Yield the fixed unavailable message, or the workflow's streamed reply."""
        if self.workflow is None:
            yield UNAVAILABLE_RESPONSE
            return
        payload = self._payload(message)
        async for chunk in self.workflow.respond(payload, self.tool):
            yield chunk

    def _payload(self, message: str) -> OutboundPayload:
        return OutboundPayload.model_validate(
            {
                "model": "openai/gpt-oss-120b",
                "messages": [
                    OutboundMessage(role="system", content=SYSTEM_PROMPT),
                    OutboundMessage(role="user", content=message),
                ],
                "scheduling_context": {
                    "current_datetime": self.clock().isoformat(),
                    "timezone": _TIMEZONE,
                    "office_hours": [],
                    "closures": [],
                    "open_slots": [],
                },
                "scheduling_rules": _SCHEDULING_RULES,
                "response_format": {"type": "json_schema", "schema_version": "1"},
            }
        )


def unavailable_chat_service() -> ChatService:
    """Return a service that always reports the fixed unavailable message."""
    return ChatService(workflow=None, tool=NoActionTool())


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
</main>
<script>
(function () {
  "use strict";
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var sendButton = document.getElementById("chat-send");
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

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var message = input.value.trim();
    if (!message) {
      return;
    }
    input.value = "";
    sendButton.disabled = true;
    fallback.setAttribute("data-visible", "false");
    appendMessage("user", message);
    var replyBody = appendMessage("assistant", "");
    setStatus("Sending...");

    fetch("/api/chat", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message })
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
            setStatus("Response complete.");
            sendButton.disabled = false;
            return;
          }
          replyBody.textContent += decoder.decode(result.value, { stream: true });
          return read();
        });
      }
      return read();
    }).catch(function () {
      showFallback();
      sendButton.disabled = false;
    });
  });

  input.focus();
})();
</script>
</body>
</html>
"""
