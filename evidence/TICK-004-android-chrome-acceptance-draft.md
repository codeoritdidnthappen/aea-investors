# TICK-004 Android Chrome local acceptance

**Status:** approved for the local-only v1 demo
**Approved:** 2026-08-19
**Scope:** Android Chrome only. Desktop Chrome remains the priority; iOS and every
other browser family are excluded from v1 acceptance.

## Evidence and decision boundary

This document is the acceptance contract for the local-only v1 demo. It deliberately
uses one reproducible Android emulator target rather than a physical-device support
matrix. Executing these cases is TICK-025; this ticket records what that execution
must prove.

| Source | Binding behavior used here |
|---|---|
| PRD NFR-19 | Keyboard navigation, labelled controls, visible focus, sufficient contrast, and non-colour-only status cues are baseline requirements. |
| PRD NFR-35 | V1 covers current stable desktop and Android Chrome, with desktop Chrome prioritized and no other browser family compatibility pass. |
| PRD FR-3, FR-4, NFR-6, NFR-7 | OpenEMR launches the iframe through OAuth/SMART; the iframe calls only the AI server; OpenEMR credentials stay out of iframe JavaScript; the iframe receives a secure HttpOnly AI-session cookie. |
| PRD FR-18, FR-19 | Response chunks stream to the iframe; dependency failure presents the native-scheduler path. |
| PRD FR-21 through FR-23 | Upload requires explicit consent, rejects invalid input clearly, and supports verifiable purge after revocation/deletion. |
| PRD FR-9 through FR-16, FR-28 | Appointment actions use existing OpenEMR endpoints and only OpenEMR confirmation establishes success. |
| ONBOARDING_CONTRACT.md | Every required value has a labelled control and explanation; manual entry remains available when upload is skipped or fails. |

## Supported local matrix

“Current stable” means the Chrome Stable version displayed in Chrome on the execution
date. The exact version and emulator API level must be captured with the TICK-025
result. The emulator reaches the local topology by using `adb reverse tcp:<port>
tcp:<port>` and opening `http://localhost:<port>` in Chrome; no public hostname,
cloud ingress, or external test environment is required.

| Target | Device class | Android version | Chrome channel/version | Required result |
|---|---|---:|---|---|
| A1 | Android Emulator, Pixel-class phone, 360–432 CSS px viewport | API 35 or later | Stable, current at execution | Full required-flow and accessibility verification against the local topology |

## Approved degradation policy

| Area | Allowed | Never allowed |
|---|---|---|
| Visual presentation | Responsive reflow, smaller typography/spacing, single-column layout, or different native file-picker appearance, provided controls remain usable without horizontal scrolling at the target viewport. | Clipped/overlapping actionable controls; hidden required information; a control that needs horizontal scrolling to use; status communicated only by colour. |
| Performance | Android may be slower than desktop; no separate Android latency target is proposed because none exists in the PRD. | A flow that cannot reach a terminal success, error, retry, or native-scheduler fallback state. |
| Input | Native Android keyboard, date picker, select UI, and file chooser may differ from desktop. | Loss of labels, validation feedback, explicit upload consent, manual-entry route, or ability to complete required fields. |
| Streaming | Chunk cadence and animation may differ from desktop. | No visible progress while a request remains active; duplicated, reordered, or permanently incomplete final output; a nonfunctional cancel/retry path when one is provided. |

## Non-negotiable accessibility behavior

These are zero-degradation requirements derived from NFR-19 and the onboarding
contract.

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

Each case passes only when it is executed on A1 against the local topology and the
specified observable result is retained with the emulator/API/Chrome record,
screenshot or screen recording, and relevant browser/network evidence. A case is not
executable until its dependent implementation exists.

| ID | Flow | Procedure | Observable pass | Observable fail |
|---|---|---|---|---|
| AND-IFRAME-01 | Iframe launch | Start the local topology, sign in as a synthetic patient, and open AI Chat through the local OpenEMR portal. | The iframe renders inside local OpenEMR; no iframe JavaScript request targets an OpenEMR endpoint; no token appears in URL, DOM, console, or network payload. | Missing/blocked iframe, direct OpenEMR request from iframe, or exposed credential. |
| AND-COOKIE-01 | AI session cookie | Launch the chat, make an AI-server request, then reload the iframe while the session is unexpired. Inspect browser storage/network headers. | The AI-server request uses its secure HttpOnly AI-session cookie; no OpenEMR bearer token is available to iframe JavaScript; the authorized session resumes according to implemented expiry behavior. | Cookie absent/inaccessible where required, bearer token exposed, or unexpired session needlessly requires reauthorization. |
| AND-STREAM-01 | Streaming and dependency fallback | Send a permitted chat request to the local AI server; then simulate local AI-server or configured-LLM unavailability. | Chunks become visible in the iframe as delivered. On dependency failure, a clear unavailable state gives a usable path to local OpenEMR’s native scheduler. | Only a stuck spinner/blank result, unusable fallback, or claimed booking success without an OpenEMR confirmation. |
| AND-UPLOAD-01 | Upload and manual path | Verify the consent control and label; decline it, then repeat with a valid synthetic image. Exercise invalid image or OCR-failure fixture and cancel/revoke before confirmation. | No upload begins before consent; decline/failure leaves manual entry completable; errors are clear; cancel/revoke makes image retrieval unavailable and extracted-value reads empty. | Upload without consent; manual path blocked; fabricated value; retrievable image or extracted values after purge trigger. |
| AND-SCHEDULE-01 | Scheduling | Check appointments, view future slots, book one, attempt a stale/conflicting selection, reschedule an existing appointment, then cancel it. | Displayed appointment facts and every success state come from validated OpenEMR responses; conflict is clear; cancellation updates status and does not delete history; cancelled appointment is absent from patient chat. | Invented or unconfirmed success, missing conflict feedback, deletion, or cancelled appointment still shown to patient. |
| AND-A11Y-01 | Baseline accessibility | On every above flow, use keyboard navigation, Android screen reader, zoom/reflow, and visual inspection of focus, labels, contrast, and status. | All non-negotiable accessibility behaviors above are satisfied. | Any missing label, invisible focus, keyboard trap, colour-only status, contrast failure, or loss of function on zoom/reflow. |

## Approval record

The local-only scope, A1 matrix, degradation policy, non-negotiable accessibility
behavior, and AND-IFRAME-01 through AND-A11Y-01 cases were approved by the project
owner on 2026-08-19. This approval defines the required checks; it is not evidence
that the checks have executed.

## Handoff

TICK-025 must execute A1 against the local topology and retain the listed evidence.
Any changed emulator/API floor, Chrome channel/version rule, permitted degradation,
or accessibility requirement requires a new approved record before it is treated as
v1 coverage.
