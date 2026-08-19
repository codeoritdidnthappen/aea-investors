# V1 Onboarding Contract

**Status:** approved
**Scope:** synthetic-data behavioral-health onboarding demo
**Traceability:** FR-5, FR-6, FR-7, FR-8, FR-21, FR-26, FR-27, FR-30

This is a deliberately minimal registration and service-routing assessment. It is
not a clinical intake, diagnostic instrument, crisis assessment, insurance form, or
staff workflow. OpenEMR remains the sole durable record. The AI server keeps no
patient answers in its session store and sends no assessment value to Groq.

## Product decisions

- The flow is for US-based adult synthetic demo patients. Do not infer citizenship,
  race, ethnicity, sex, gender identity, sexual orientation, diagnosis, medications,
  trauma history, insurance, emergency contact, or payment information.
- The user may skip document upload and enter identity fields manually.
- Every required value uses a labelled control and a short explanation of why it is
  collected. The assistant must not give clinical advice or make suitability claims.
- A user may leave and resume an incomplete flow. Completion requires all required
  fields and explicit confirmation of the review screen.

## Field contract and flow order

| Order | Field | Required | Input and validation | Record behavior | Handling |
|---:|---|:---:|---|---|---|
| 1 | Identity-document processing consent | Only to upload | Unticked checkbox. The label states that a synthetic ID image will be read locally to prefill identity fields and discarded after confirmation. | Consent event only; it is not an assessment answer. | Deterministic/local |
| 2 | Legal given name | Yes | Text; 1–100 non-whitespace characters. Prefill only from OCR after consent; user confirms or edits. | Write with identity confirmation. | Deterministic/local |
| 3 | Legal family name | Yes | Text; 1–100 non-whitespace characters. Prefill only from OCR after consent; user confirms or edits. | Write with identity confirmation. | Deterministic/local |
| 4 | Date of birth | Yes | Date; valid calendar date, not future, and patient is at least 18 on submission. Prefill only from OCR after consent; user confirms or edits. | Write with identity confirmation. | Deterministic/local |
| 5 | Mailing address | Yes | Street line 1, city, two-letter US state/territory, and five- or nine-digit ZIP code are required. Street line 2 is optional. Values may be prefilled only from OCR after consent; user confirms or edits. | Write with identity confirmation. | Deterministic/local |
| 6 | Preferred contact method | Yes | Select one: phone, email, or portal message. The corresponding contact value must be syntactically valid: E.164-compatible US phone number or standard email format. Portal message needs no additional value. | Part of assessment draft; do not overwrite an OpenEMR contact field until its endpoint behavior is mapped. | Deterministic/local |
| 7 | What would you like help with? | Yes | Select one: `Counseling or therapy`, `Psychiatric evaluation or medication support`, `Both`, or `Not sure yet`. No free-text explanation. | Part of assessment draft and final assessment. | Deterministic/local |
| 8 | Visit preference | Yes | Select one format: `In person`, `Video`, `Either`, or `Not sure`; select one time window: `Weekday morning`, `Weekday afternoon`, `Weekday evening`, `Weekend`, or `No preference`. | Part of assessment draft and final assessment; it is a preference, not a booking. | Deterministic/local |
| 9 | Language or accessibility accommodation | No | Multi-select: `Language interpreter`, `Hearing accommodation`, `Vision accommodation`, `Mobility accommodation`, or `Other accommodation`; optional 200-character detail appears only after `Other accommodation`. | Part of assessment draft and final assessment. | Deterministic/local |

The confirmation screen presents the eight data fields, excluding the upload-consent
event. The user may return to any field before selecting **Confirm and complete**.

## Draft and completion semantics

1. Start a native OpenEMR assessment draft when the user begins the flow, subject to
   the endpoint result from TICK-001. Store only the workflow cursor in AI-server
   SQLite.
2. After each valid user submission, checkpoint that field in the OpenEMR draft.
   Never checkpoint OCR output until the user has confirmed or edited it.
3. On an OCR failure, partial result, declined consent, or upload cancellation, leave
   all affected identity inputs blank and continue with manual entry. Never guess.
4. The draft remains incomplete until every required field is valid and the user
   confirms the review screen.
5. On confirmation, write confirmed demographics and finalize the structured
   assessment through existing OpenEMR endpoints. Show completion only after both
   OpenEMR operations succeed. On failure, retain the draft and show a retry message.

## Conversational boundary

The chat may explain the current step, restate a selected option, and answer generic
process questions. It must not interpret responses, infer missing values, create a
clinical summary, or make treatment recommendations. All field capture, validation,
draft writes, and completion checks are deterministic local operations. Any
model-mediated language is limited to non-patient, non-clinical phrasing and cannot
receive assessment values under the outbound privacy policy.

## Supportive-content rules

| Trigger | Detection | Approved content | No-trigger behavior |
|---|---|---|---|
| Long pause | 120 seconds with no interaction while a field is active; show once per field. | “Take your time. Your progress is saved, and you can continue when you’re ready.” | Show nothing. Do not start a countdown or repeat the prompt. |
| Upload failure | Local client/server validation, OCR error, or low-confidence result. | “That image didn’t work, but you can continue by entering your details manually. We won’t guess any missing information.” | Show nothing. Successful or partial uploads proceed to the confirmation fields. |
| Distress intent | Local, conservative intent detection of an explicit expression of distress; it does not diagnose risk. | “I’m sorry this feels difficult. You can pause or continue later. If you might hurt yourself or are in immediate danger, call or text 988 in the U.S., call 911, or contact local emergency services.” | Show nothing. Never surface crisis language solely because of a demographic, selection, pause, or upload outcome. |

Support content does not change required fields, completion rules, or appointment
eligibility. It is not clinical advice and does not replace emergency care.

## Fixture cases

| Case | Expected result |
|---|---|
| Confirmed complete OCR identity with all required selections | Demographics and completed assessment are written after review confirmation. |
| Partial OCR result | Extracted values require confirmation; blank values require manual entry; completion remains available. |
| Declined upload consent | No upload starts; manual identity entry remains available. |
| Invalid date, address, phone, or email | Field-level error; draft does not advance for that field. |
| User under 18 | Date-of-birth validation blocks this adult-demo flow and directs the user to the standard registration path. |
| Contact preference is portal message | No phone or email value is required. |
| Other accommodation selected | Detail is optional, limited to 200 characters, and is saved only in the OpenEMR draft/assessment. |
| 120-second inactive field | One long-pause message appears; no message is shown before the threshold. |
| Upload error or low-confidence OCR | Manual-entry supportive content appears; no identity value is fabricated. |
| Explicit distress phrase | Distress supportive content appears; an ordinary question or non-distressed answer does not trigger it. |

## Approval record

The product owner approved this exact minimal v1 contract on 2026-08-18. The flow
may proceed to implementation once the required OpenEMR endpoint mapping is complete.
