# TICK-024: Desktop Chrome E2E, third pass (2026-08-20)

Re-attempted live now that TICK-038, TICK-039, TICK-040, TICK-041, and
TICK-042 (all found and fixed during this same pass) are resolved. Real
desktop Chrome browser automation throughout, no scripted API calls unless
explicitly noted as a disposable verification aid.

## Verified this pass

1. **Login, nav tile, iframe, session, streaming, accessibility** --
   unchanged from the prior two passes (`DESKTOP_E2E_EVIDENCE.md`,
   `DESKTOP_E2E_EVIDENCE_2.md`); not re-exercised in full detail here.

2. **Real appointment cancellation through the actual chat UI** -- seeded a
   fresh cancel-eligible appointment (`pc_eid=10`), sent "Can you cancel my
   upcoming appointment?" through the real message box, received "Cancelled
   and confirmed by OpenEMR: appointment
   a28d35f6-13f7-446a-96d7-9d3b02a0b8b9 is now cancelled.", confirmed
   `pc_apptstatus='x'` in the database. TICK-039/TICK-041's fixes both
   verified live end to end, not just via unit tests.

3. **Full guided-onboarding conversation, completed** -- every turn sent
   through the real message box: contact method, help type, visit
   format/time, accommodations, given name, family name, date of birth,
   mailing address, review, `CONFIRM`. Response: "Thanks, Avery! Your
   onboarding is complete and saved to your OpenEMR record." Confirmed a
   real `patient_data` row (`fname='Avery'`, `lname='Subjecttest'`,
   `DOB='1995-04-12'`, `street='100 Maple Ave, Springfield, IL 62704'`).
   This is the fix delivered by **TICK-042** (found during this same pass:
   the demographics write route was structurally unreachable for a genuine
   patient token, the same class of bug TICK-040 found for booking) --
   full details and root cause in
   `evidence/TICK-042/DEMOGRAPHICS_WRITE_ROUTE_EVIDENCE.md`.

   Two testing-methodology artifacts along the way, neither a product bug:
   a mid-session container restart (required to deploy the TICK-042 code
   fix) cleared in-memory identity-field state and required re-answering
   those fields (documented, pre-existing, restart-durable-for-drafts-only
   behavior); and the first completion attempt used a session token minted
   before the new OAuth scope was registered, correctly rejected with
   `scope patient/demographics.u not in access token` -- confirming the new
   route's scope enforcement works, not a bug.

## Still not reachable: OCR confirmation

Investigated directly rather than attempted live: `ai_server/ocr/` (OCR
extraction, TICK-014/015) and `ai_server/onboarding/` (guided onboarding,
TICK-017/035) were each built and independently tested, but never wired
together. Onboarding's identity capture
(`ai_server/app/onboarding_chat.py:66-93`, `FIELD_PROMPTS`) asks the patient
to type given name, family name, date of birth, and address directly --
there is no upload step anywhere in the flow. Confirmed by grep: nothing
under `ai_server/app/*.py` imports `ai_server.ocr`, and `ai_server/app/main.py`
registers no upload/OCR-related HTTP route. This is a missing integration,
not a bug -- filed as **TICK-044**.

## What remains unverified

OCR confirmation, blocked on TICK-044 (a build ticket, not fixable within
this verification ticket's own scope). Every other AC item this ticket
lists -- login, iframe launch, session, streaming, onboarding, appointment
operations (book/cancel), fallback, keyboard/accessibility -- has now been
verified live in this or a prior pass.

## Recommendation

Keep TICK-024 `blocked` on TICK-044 rather than mark it `done` with an
unmet AC. Once TICK-044 lands, this is the only remaining case to re-run.
