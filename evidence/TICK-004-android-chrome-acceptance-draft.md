# TICK-004 Android Chrome acceptance draft

**Status:** pending product approval
**Prepared:** 2026-08-18
**Scope:** Android Chrome only. Desktop Chrome remains the priority; iOS and every
other browser family are excluded from v1 acceptance.

## Evidence and decision boundary

This document translates the existing requirements into a candidate Android Chrome
acceptance contract. It is not an approval record: no product review, sign-off, test
environment, or executed Android result was available when it was prepared.

| Source | Binding behavior used here |
|---|---|
| PRD NFR-19 | Keyboard navigation, labelled controls, visible focus, sufficient contrast, and non-colour-only status cues are baseline requirements. |
| PRD NFR-35 | V1 covers current stable desktop and Android Chrome, with desktop Chrome prioritized and no other browser family compatibility pass. |
| PRD FR-3, FR-4, NFR-6, NFR-7 | OpenEMR launches the iframe through OAuth/SMART; the iframe calls only the AI server; OpenEMR credentials stay out of iframe JavaScript; the iframe receives a secure HttpOnly AI-session cookie. |
| PRD FR-18, FR-19 | Response chunks stream to the iframe; dependency failure presents the native-scheduler path. |
| PRD FR-21 through FR-23 | Upload requires explicit consent, rejects invalid input clearly, and supports verifiable purge after revocation/deletion. |
| PRD FR-9 through FR-16, FR-28 | Appointment actions use existing OpenEMR endpoints and only OpenEMR confirmation establishes success. |
| ONBOARDING_CONTRACT.md | Every required value has a labelled control and explanation; manual entry remains available when upload is skipped or fails. |

## Candidate supported matrix — requires approval

The product owner must approve the exact device/OS rows before this becomes a support
commitment. “Current stable” means the Chrome Stable version displayed in Chrome on
the execution date; the exact version and Android security-patch level must be
captured in the verification evidence. A stable channel version is unsupported when
Chrome reports it is no longer current at execution time.

| Target | Device class | Android version | Chrome channel/version | Required result |
|---|---|---:|---|---|
| A1 | Google Pixel phone, 360–432 CSS px viewport | Android 14 or later | Stable, current at execution | Full required-flow and accessibility verification |
| A2 | Samsung Galaxy phone, 360–432 CSS px viewport | Android 13 or later | Stable, current at execution | Full required-flow and accessibility verification |

No assertion is made that these particular models or OS floors are approved. The
review must either approve these rows or replace them with the device/OS inventory
that product will support. Physical devices are required; responsive emulation may
supplement but cannot satisfy either row.

## Candidate degradation policy — requires approval

| Area | Allowed only if approved | Never allowed |
|---|---|---|
| Visual presentation | Responsive reflow, smaller typography/spacing, single-column layout, or different native file-picker appearance, provided controls remain usable without horizontal scrolling at the target viewport. | Clipped/overlapping actionable controls; hidden required information; a control that needs horizontal scrolling to use; status communicated only by colour. |
| Performance | Android may be slower than desktop; no separate Android latency target is proposed because none exists in the PRD. | A flow that cannot reach a terminal success, error, retry, or native-scheduler fallback state. |
| Input | Native Android keyboard, date picker, select UI, and file chooser may differ from desktop. | Loss of labels, validation feedback, explicit upload consent, manual-entry route, or ability to complete required fields. |
| Streaming | Chunk cadence and animation may differ from desktop. | No visible progress while a request remains active; duplicated, reordered, or permanently incomplete final output; a nonfunctional cancel/retry path when one is provided. |

## Non-negotiable accessibility behavior

These are proposed as zero-degradation requirements derived from NFR-19 and the
onboarding contract; product approval must confirm they remain non-negotiable.

- A Bluetooth or USB keyboard can reach every interactive iframe control in a logical
  order, activate it, and dismiss any modal without a pointer.
- Every interactive and required input has a programmatic label; requiredness,
  instructions, validation errors, upload consent, and scheduling outcome are
  understandable without relying on colour alone.
- The focused control is visibly distinguishable at each target viewport, including
  after scrolling and within the iframe.
- Text, controls, error messages, streaming status, and scheduling outcomes have
  sufficient contrast under the product’s selected contrast standard; the selected
  standard and measurement evidence must be recorded at verification.
- The user can zoom/reflow with Android Chrome without loss of content or required
  functionality, and can use the mobile screen reader to identify labels and state.

## Android verification cases

Each case passes only when it is executed on both approved matrix rows and the
specified observable result is retained with a device/version record, screenshot or
screen recording, and relevant browser/network evidence. A case is not executable
until the dependent implementation and deployed test environment exist.

| ID | Flow | Procedure | Observable pass | Observable fail |
|---|---|---|---|---|
| AND-IFRAME-01 | Iframe launch | Sign in as a synthetic patient and open AI Chat. | The iframe renders inside OpenEMR; no iframe JavaScript request targets an OpenEMR endpoint; no token appears in URL, DOM, console, or network payload. | Missing/blocked iframe, direct OpenEMR request from iframe, or exposed credential. |
| AND-COOKIE-01 | AI session cookie | Launch the chat, make an AI-server request, then reload the iframe while the session is unexpired. Inspect browser storage/network headers. | The AI-server request uses its secure HttpOnly AI-session cookie; no OpenEMR bearer token is available to iframe JavaScript; the authorized session resumes according to implemented expiry behavior. | Cookie absent/inaccessible where required, bearer token exposed, or unexpired session needlessly requires reauthorization. |
| AND-STREAM-01 | Streaming and dependency fallback | Send a permitted chat request; then simulate AI-server or external-LLM unavailability. | Chunks become visible in the iframe as delivered. On dependency failure, a clear unavailable state gives a usable path to OpenEMR’s native scheduler. | Only a stuck spinner/blank result, unusable fallback, or claimed booking success without an OpenEMR confirmation. |
| AND-UPLOAD-01 | Upload and manual path | Verify the consent control and label; decline it, then repeat with a valid synthetic image. Exercise invalid image or OCR-failure fixture and cancel/revoke before confirmation. | No upload begins before consent; decline/failure leaves manual entry completable; errors are clear; cancel/revoke makes image retrieval unavailable and extracted-value reads empty. | Upload without consent; manual path blocked; fabricated value; retrievable image or extracted values after purge trigger. |
| AND-SCHEDULE-01 | Scheduling | Check appointments, view future slots, book one, attempt a stale/conflicting selection, reschedule an existing appointment, then cancel it. | Displayed appointment facts and every success state come from validated OpenEMR responses; conflict is clear; cancellation updates status and does not delete history; cancelled appointment is absent from patient chat. | Invented or unconfirmed success, missing conflict feedback, deletion, or cancelled appointment still shown to patient. |
| AND-A11Y-01 | Baseline accessibility | On every above flow, use keyboard navigation, Android screen reader, zoom/reflow, and visual inspection of focus, labels, contrast, and status. | All non-negotiable accessibility behaviors above are satisfied. | Any missing label, invisible focus, keyboard trap, colour-only status, contrast failure, or loss of function on zoom/reflow. |

## Review and approval record

Approval is pending. Product must fill this record after reviewing the matrix,
degradation policy, and every verification case; an approver name/date alone is not
evidence that the cases passed.

| Decision | Required evidence | Status |
|---|---|---|
| Supported device/OS/Chrome matrix | Named approver, review date, accepted or amended A1/A2 rows | Pending |
| Allowed degradation | Named approver, review date, accepted or amended policy rows | Pending |
| Non-negotiable accessibility behavior | Named approver, review date, accepted or amended requirements | Pending |
| Android verification cases | Product acknowledgement that AND-IFRAME-01 through AND-A11Y-01 are the required checks | Pending |

## Handoff

After approval, TICK-025 must execute the approved matrix and retain the listed
evidence. Any changed device, OS floor, Chrome channel/version rule, permitted
degradation, or accessibility requirement requires a new approved record before it
is treated as v1 coverage.
