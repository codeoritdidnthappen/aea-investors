# TICK-055 decisions

Two of this ticket's acceptance criteria ask for a decision to be made *explicitly and
recorded*, not merely for code to exist. This is that record. A third decision (the
portal sign-out mechanism, AC2) is recorded separately in `PORTAL_LOGOUT_MECHANISM.md`
because it needed its own measurements.

> Note on where this lives: the ticket text says these should be "recorded in the
> ticket." A build worker may not modify ticket files — the orchestrator owns backlog
> state — so they are recorded here and referenced from the code that implements them.
> Folding them into `tickets/TICK-055-*.md` is a backlog-side action.

---

## AC6 — the 8-hour TTL stays absolute, and is announced

**Decision: keep the hard 8-hour cut. Do not redeem the refresh token. Warn the patient
before it lands.**

The facts the decision rests on:

- `session_ttl` is 8 hours (`ai_server/app/auth.py`).
- `create_session` stamps `expires_at = now + ttl` exactly once and nothing ever
  updates it. `save_cursor` deliberately writes `WHERE expires_at > ?` and never
  touches the column.
- A refresh token **is** stored, encrypted (`refresh_nonce`/`refresh_ciphertext`), and
  `offline_access` is in the requested scopes — but no code path anywhere in
  `ai_server/` ever reads it back or issues a `grant_type=refresh_token` request.

**Why not redeem the refresh token.** Renewal is not a small addition to this ticket.
It needs a refresh grant on `OpenEmrOAuthClient`, a re-encrypt-and-restamp path on
`SessionStore`, a policy for refresh-token rotation (whether OpenEMR returns a new one,
and what happens to a session whose refresh fails mid-conversation), and a decision
about whether renewal should be bounded by an absolute maximum anyway. It also cannot
be honestly live-verified inside this ticket: proving renewal works means driving a
session across an 8-hour boundary against a real provider. Shipping it untested inside
a privacy fix would be worse than not shipping it. It is a coherent separate ticket,
and the stored refresh token is already there when someone writes it.

**Why the absolute cut is defensible on its own.** A patient-facing chat holding
delegated credentials to a medical record has a positive reason to bound how long those
credentials live regardless of activity: an idle-extended session on a shared machine
is precisely the exposure this ticket exists to reduce. An absolute cap is the
conservative choice, not merely the cheap one.

**What was implemented for "the patient is warned before it lands."** `_expiry_notice`
(`ai_server/app/main.py`) prefixes a plain-language notice to the reply of any chat turn
taken inside the final `expiry_warning_window` (default 30 minutes):

> Heads up: this chat session ends in about 12 minutes, and you'll need to sign in from
> your patient portal again to keep chatting.

Delivered on the turn itself rather than pushed from a new endpoint, for two reasons:
it needs no polling and no second network call from the chat page (which keeps the
page's single-`fetch` property that `test_ac1_chat_page_only_fetches_the_ai_servers_own_relative_endpoint`
pins), and it appears in the transcript where the patient is already reading rather
than in a banner they may have scrolled past.

Pinned by `test_ac6_a_session_near_its_hard_cut_warns_the_patient_in_the_reply`,
`test_ac6_a_session_with_time_left_is_not_warned`, and
`test_ac6_the_ttl_is_never_extended_by_activity` — the last of which exists so that if
a future ticket *does* implement renewal, the change to this behaviour is deliberate
and visible in a diff rather than silent.

**Known limit.** The warning only reaches a patient who takes a turn inside the final
window. Someone who leaves the panel open and idle is still cut off without seeing it.
Fixing that needs a client-side timer, and therefore a way for the page to learn the
expiry — a second endpoint or a response header. Not done here.

---

## AC4 / AC5 — smaller decisions worth stating

**Logout is `POST` only, and there is deliberately no `GET /logout`.** A GET carries no
`Origin` a browser will not let a page forge, so a state-changing GET would be
reachable from any off-origin `<img>` or redirect — the CSRF sink AC4 forbids. `GET
/api/logout` returns `405`, verified live. The cost is that there is no bookmarkable
sign-out URL; nothing in the product needs one.

**Logout is idempotent and reports nothing about the handle.** Every call returns `204`
whether the session existed, had already expired, or was never real. A distinguishable
response would be an oracle for guessing live handles.

**The sweep runs at startup *and* on an interval, not one or the other.** AC5 permits
either. Startup alone does not bound retention for a container that runs for weeks
without a restart, which is the normal case; an interval alone leaves rows stranded
across a restart until the first tick. Both is a few lines
(`_sweep_expired_sessions`, `sweep_interval`, default 1 hour) and closes both gaps.

**`delete_session` takes no `now`.** There is no clock at which a logged-out session's
encrypted tokens should stay on disk, so the delete is unconditional on `expires_at` —
an already-expired-but-unswept row is removed too.

**Logout also drops in-process state.** `OnboardingChatService._sessions` and
`AddressChatService`'s pending updates hold the patient's own half-finished answers
(name, date of birth, address) in memory keyed by the handle. Deleting the encrypted
tokens while leaving those behind would not be a logout, so `discard(handle)` is called
on both. `OnboardingChatService.discard` is new; it mirrors the one
`AddressChatService` already had.
