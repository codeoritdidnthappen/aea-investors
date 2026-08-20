# TICK-024 — desktop Chrome E2E re-attempt, 2026-08-20

**Executed:** real desktop Chrome via browser automation (not a sandboxed
worker) against the live local Docker topology, after TICK-032 and TICK-033
(this ticket's prior blockers) landed.

## Result

Substantially further than the first attempt (`DESKTOP_E2E_EVIDENCE.md`), but
still not complete -- two new, real, live findings block full closure. Both
are filed as their own tickets rather than fixed mid-verification.

## What was verified live

1. **Portal login** via the classic login form (`AverySubjecttest1`),
   landing on `portal/home.php`.
2. **The AI Chat entry is now a genuine dashboard tile** (TICK-032's fix):
   present among the other 10 dashboard buttons (Clinical Documents,
   Appointments, Secure Messaging, Health Snapshot, Profile, Billing
   Summary, Medical Reports, Settings, Help, Logout, AI Chat) -- no
   scrolling required, resolving Finding 1 from the prior attempt.
3. **Clicking the tile embeds a genuine, working iframe**: labeled "AI
   Chat", rendering the real OAuth login form and, after logging in, the
   real consent screen -- both inline, confirmed via direct accessibility-tree
   inspection, not just a visual screenshot. (Could not complete the OAuth
   dance entirely inside the iframe here -- a known tool limitation with
   cross-origin iframe scroll/interaction, already documented in the prior
   evidence file; worked around the same way, via the identical flow in its
   own tab, which is the real, supported non-iframe entry point
   `oauth/launch` redirects through regardless.)
4. **Patient-context consent, not staff-context** (TICK-033's fix): the
   consent screen shows four small `PATIENT`-tagged resource cards
   (Appointment, Patient, appointment, assessment), not the old staff-style
   full-CRUD grid -- auto-approved with no manual admin step, resolving
   Finding 2 from the prior attempt.
5. **Session and streaming**: a real `/api/chat` turn showed `Status:
   Receiving response...` then `Status: Response complete.` live -- genuine
   token streaming, not a single blocking response.
6. **Booking correctly reports no availability**: "I want to schedule an
   appointment" returned "No scheduling action is available." This is the
   pre-existing, deliberately honest `NoMappedCandidateSource` behavior
   (no OpenEMR office-hours/availability endpoint on this pinned release,
   `ADR-3`) -- not a new bug, not a regression.
7. **Accessibility**: the chat's accessibility tree uses correct semantic
   roles throughout -- `status` for the live status line, `log` (labeled
   "Conversation") containing `listitem`s per turn, `alert` for the
   dependency-failure fallback message, and a proper `form`/`label`/
   `textbox`/`button[type=submit]`. Full keyboard operability confirmed:
   Tab reached the message field, typed text, Tab reached Send, Enter
   submitted -- a new turn appeared with no mouse interaction at any point.
8. **Dependency-failure fallback**: repeatedly observed live this session
   (see TICK-038) -- when a downstream OpenEMR call fails, the UI renders
   a clear `alert`-role message ("Chat unavailable... Please close this
   chat panel and use the appointment scheduling option in your OpenEMR
   portal menu instead.") instead of crashing or leaking an error/stack
   trace to the patient.

## Finding 1 (new) — onboarding blocked by TICK-038

Starting onboarding ("I'd like to start my onboarding.") reaches a real,
successful OpenEMR write (`POST /apis/default/api/portal/patient/assessment`
returns `201`, confirmed by a real row in `aeai_assessment_draft`), but the
AI server itself fails to parse that successful response and the turn ends
in the dependency-failure fallback shown above. Filed as **TICK-038** (open).
OCR confirmation, which comes after onboarding in the intended flow, could
not be reached as a result.

## Finding 2 (new) — cancellation never selects a real, available appointment

A real appointment was seeded via `AppointmentService::insert()` (the same
real OpenEMR call the booking tool itself uses) for the logged-in patient.
Confirmed live that `GET /apis/default/fhir/Appointment` returns a real,
non-empty bundle for it (1240 bytes vs. 195 for an empty one), meaning the
appointment genuinely reaches `scheduling_context.current_appointments` in
the payload built for the LLM. Despite `cancellation_enabled=True` and a
system prompt that correctly instructs the model to reference an
`appointment_token`, asking the chat to cancel the appointment still
produced the generic no-action fallback rather than a real cancellation.
Filed as **TICK-039** (open); root cause not yet isolated (needs the actual
Groq request/response captured for a reproducing turn).

## What remains unverified

OCR confirmation (blocked by Finding 1), and completed appointment
cancellation (blocked by Finding 2). Onboarding cannot be exercised past its
first turn until TICK-038 lands; cancellation cannot be exercised to
completion until TICK-039 lands.

## Recommendation

Re-run this ticket's remaining cases (OCR confirmation, cancellation
completion) once TICK-038 and TICK-039 both land.
