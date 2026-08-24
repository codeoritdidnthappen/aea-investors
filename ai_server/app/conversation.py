"""The state one patient's conversation carries from turn to turn.

TICK-063 AC4. A half-finished address, an onboarding position, a pending confirmation
and the anonymous tokens this session was offered all have to survive to the next turn,
and none of them may be *re-derived* by reading the transcript back with a pattern --
that is the mechanism this ticket exists to remove. So each is recorded here at the
moment it happens, by the code that made it happen, and handed to the model next turn as
context it reads rather than state it reconstructs.

**Why in-process, keyed by handle.** ARCHITECTURE.md Sec. 5 and NFR-33 reserve the
`sessions.cursor` column for the onboarding draft id and nothing else: no patient value
and no transcript may be persisted there. The onboarding position therefore stays where
it already lives -- `SessionStore.save_cursor`, an opaque draft id, with the answers
themselves in OpenEMR -- and everything here is held in this process's memory only, the
same discipline the deleted `OnboardingChatService._sessions` and
`AddressChatService._sessions` followed before TICK-065. A restart loses a pending
confirmation, which is correct: the patient is asked again and nothing was saved.

**Why it must be discarded on logout.** `pending` holds validated values the patient
typed and `turns` holds their own words. Deleting the session row while leaving those in
memory would keep patient data alive past the logout that was supposed to end it. Since
TICK-065 this is the only in-memory patient state `/api/logout` has to drop; it used to
guard the two deterministic services' stores as well.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ai_server.llm.validation import ValidatedWrite
from ai_server.scheduling.appointments import AnonymousAppointmentToken
from ai_server.scheduling.slots import AnonymousSlotToken

# How many earlier messages the model is shown. Enough for "actually, make it 2004" to
# have something to refer to, and bounded because the transcript is patient text held in
# memory: an unbounded one would grow for the whole 8-hour session TTL. The state the
# turn actually depends on -- the pending write, the offered tokens, the question last
# asked -- is recorded in its own field precisely so that trimming this cannot lose it.
MAX_TRANSCRIPT_MESSAGES = 12

# An idle conversation's state is dropped rather than resumed, carrying over the
# 10-minute pending-update TTL the deleted `AddressChatService` used: a confirmation the
# patient walked away from half an hour ago should not be waiting for a stray "yes".
DEFAULT_CONVERSATION_TTL = timedelta(minutes=10)


@dataclass
class ConversationState:
    """Everything one handle's conversation knows that this turn did not tell it."""

    # The last `MAX_TRANSCRIPT_MESSAGES` messages, oldest first, as (role, text) pairs
    # in the chat-completions vocabulary ("user"/"assistant").
    turns: list[tuple[str, str]] = field(default_factory=list)
    # The change read back to the patient and not yet confirmed, together with the exact
    # words it was read back in. Holding the `ValidatedWrite` itself -- not the model's
    # proposal -- is what lets the next turn's agreement execute the validator's values.
    pending: ValidatedWrite | None = None
    pending_prompt: str | None = None
    # The tokens this session was *issued* (`SlotDiscoveryService`,
    # `AppointmentDiscoveryService`), kept as issued rather than re-derived: listing
    # again mints fresh tokens and invalidates the ones the model was already shown.
    offered_slots: tuple[AnonymousSlotToken, ...] = ()
    offered_appointments: tuple[AnonymousAppointmentToken, ...] = ()
    # The question the assistant last put to the patient, so an answer like "no" can be
    # understood as an answer to it (the corpus's own `asked` field).
    asked: str | None = None
    # The live OCR upload this patient just made, if any.
    upload_id: str | None = None
    touched_at: datetime | None = None

    def record(self, role: str, text: str) -> None:
        """Append one message and keep the transcript bounded."""
        self.turns.append((role, text))
        del self.turns[:-MAX_TRANSCRIPT_MESSAGES]

    def earlier_turns(self) -> list[tuple[str, str]]:
        """Every recorded message except the most recent assistant one.

        That last message is carried separately as `asked`, in the framing the
        acceptance corpus measured ("You have just asked the patient: ..."). Trimming it
        here is what keeps it from reaching the model twice.
        """
        return self.turns[:-1]

    def clear_pending(self) -> None:
        self.pending = None
        self.pending_prompt = None


@dataclass
class ConversationStore:
    """Per-handle conversation state, held in this process and nowhere else."""

    ttl: timedelta = DEFAULT_CONVERSATION_TTL
    _states: dict[str, ConversationState] = field(default_factory=dict)

    def state(self, handle: str, now: datetime) -> ConversationState:
        """Return this handle's state, starting a fresh one if it went idle."""
        existing = self._states.get(handle)
        if existing is not None:
            if existing.touched_at is not None and now - existing.touched_at > self.ttl:
                # Idle past the TTL: drop it whole rather than resume half of it. A
                # pending confirmation that survived would be the more dangerous half.
                existing = None
            else:
                existing.touched_at = now
        if existing is None:
            existing = ConversationState(touched_at=now)
            self._states[handle] = existing
        return existing

    def discard(self, handle: str) -> None:
        """Forget this handle's conversation entirely (logout, TICK-055 AC1)."""
        self._states.pop(handle, None)
