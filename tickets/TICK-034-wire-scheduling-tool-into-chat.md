---
id: TICK-034
title: "feat(chat): replace the no-op tool with real appointment booking"
type: feature
epic: EPIC-07
priority: P1
estimate: M
depends_on: [TICK-010, TICK-031, TICK-035]
labels: [chat, scheduling, langgraph]
source: [FR-11, FR-12, FR-16, FR-20]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/71
builder_commit: 8f7a750
---
## Context

`ai_server/app/chat.py`'s `NoActionTool` is the only `AuthoritativeTool`
`ChatService` ever constructs, and it performs no OpenEMR operation for any
planned intent -- every chat turn ends in "No scheduling action is available
yet in this demo." regardless of what the user asks. Its own docstring
explains why: "Booking, rescheduling, and cancellation are unimplemented
(TICK-020 is blocked)". That was true when TICK-010 built the planning/
streaming pipeline, but TICK-020 was later narrowed and its booking scope
split into TICK-031, which landed a real, tested
`ai_server.scheduling.booking.BookingService` -- never called from `chat.py`.
Confirmed live 2026-08-20: a real patient chat session reaches Groq, gets a
valid plan, and still always receives the same no-action summary for a
`book` intent, even though `BookingService` exists and passes its own tests.

**First attempt at this ticket (2026-08-20) blocked** on a real gap: nothing
retrieved the logged-in patient's delegated OpenEMR access token for
in-process use -- `BookingService.book(access_token, ...)` needs one, and
`ChatService`/`AuthoritativeTool.execute()` never received it.  TICK-035
(guided onboarding, landed same day) needed the identical capability and
built it as shared, reusable infrastructure:
`SessionStore.access_token(handle, now) -> str | None`
(`ai_server/app/auth.py`) decrypts and returns the delegated token for an
active session; `main.py`'s `/api/chat` handler already loads the session by
`handle` for auth. Use that same method here instead of inventing a second
retrieval path.

Open slot discovery (`ai_server.scheduling.slots.SlotDiscoveryService`,
`AnonymousSlotStore`) is also unwired: `ChatService._payload()` hardcodes
`scheduling_context.open_slots = []`, so the model never has real slots to
offer regardless of what a tool could do.

Cancellation is **out of scope for this ticket** (see TICK-036): it needs an
anonymous appointment-targeting token mirroring `AnonymousSlotStore`'s
existing slot-token pattern, which doesn't exist yet and is real design work
in its own right, not a same-shaped follow-on to booking. Reschedule has no
backend at all and stays on TICK-020, permanently blocked.

## Acceptance Criteria

- [ ] `POST /api/chat`'s handler retrieves the session's access token via
      `SessionStore.access_token(handle, now)` and makes it available to
      `ChatService`/the tool layer for the duration of one turn; it is never
      persisted, logged, or cached outside that call.
- [ ] `ChatService._payload()` populates `scheduling_context.open_slots` from
      `SlotDiscoveryService` for the logged-in patient, not a hardcoded
      empty list.
- [ ] A new `AuthoritativeTool` implementation executes `book` plans (with a
      `slot_token`) through `BookingService.book(access_token, ...)`, and
      falls back to the existing no-action summary for `reschedule`,
      `cancel` (TICK-036), and any `book` intent missing a `slot_token`.
- [ ] A successful booking produces a `public_summary` describing only the
      OpenEMR-confirmed outcome (FR-20); a conflict or stale-slot response is
      reported clearly and invents no commitment.
- [ ] `main.py`'s `_build_chat_service`/turn-handling constructs the real
      tool (with the OpenEMR-facing HTTP client already used for OAuth/
      health, `verify=False` per the established local self-signed-cert
      exception) instead of `NoActionTool`, for every environment where the
      required OpenEMR settings are present.

## Testing

Unit-test the new tool against a fake `BookingService` for success, conflict,
and missing-slot-token cases, plus the existing reschedule/cancel fallback.
Run a live end-to-end chat turn against the local Docker stack (real patient
login, real slot, real book) and capture the evidence, matching TICK-031's
own live-proof bar. CI must be green.

## Out of Scope

Cancellation (TICK-036). Reschedule (TICK-020, permanently blocked -- no
OpenEMR service method exists). Changing `BookingService`,
`SlotDiscoveryService`, or `SessionStore.access_token` themselves.
