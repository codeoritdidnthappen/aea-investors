---
id: TICK-028
title: "spike(auth): obtain and prove a patient-context token locally"
type: spike
epic: EPIC-01
priority: P1
estimate: M
depends_on: [TICK-001, TICK-008, TICK-022]
labels: [openemr, oauth, smart, privacy]
source: [FR-3, FR-26, NFR-25, NFR-30]
status: todo
---

## Context

Every write the product performs today was probed with the **password grant as the
seeded local admin** (`evidence/TICK-001/PROBE_EVIDENCE.md`). That is a staff-context
token. It is not acceptable, including locally: the local environment must exercise the
same authorization boundary the product claims, or the boundary is untested fiction.

`ENDPOINT_MATRIX.md` already names the unresolved row. `patient/Patient.cud` is
syntactically documented, but the FHIR `PUT` route has no patient-binding branch, unlike
the FHIR `GET` which does. Two outcomes are possible and they point opposite ways:

- The route enforces binding → the product has a legitimate patient-context write path
  and TICK-016 should be re-probed under it.
- The route does not enforce binding → a `patient/` token can write **another patient's
  chart**. That is a finding about OpenEMR, not a feature to build on, and the product
  must not use the route at all.

This ticket settles which. It is a prerequisite for trusting TICK-016 and for any
claim the demo makes about acting as the patient.

## Acceptance Criteria

- [ ] A synthetic patient exists with **portal login credentials**, provisioned by a
      documented, repeatable path.
- [ ] An OAuth client is registered requesting patient-scoped SMART scopes only; the
      registration command and resulting scope string are recorded.
- [ ] A token is obtained through **authorization_code + PKCE**, where the *patient*
      authenticates at OpenEMR's own login and consents. No password grant. No `users`
      row. No admin credential anywhere in the flow.
- [ ] The token's bound patient is confirmed from the token response (`patient` claim)
      and by reading own demographics successfully.
- [ ] **Negative binding test:** the same token is used against a *second* synthetic
      patient's chart. Both the read and the write path are attempted. The result of
      each is recorded verbatim.
- [ ] The write path (`PUT /api/patient/{uuid}` and `PUT /fhir/Patient/{uuid}`) is
      attempted against own chart and against the other chart. Four outcomes recorded.
- [ ] Evidence lands in `evidence/TICK-028/` under the same redaction policy as
      TICK-001 — no tokens, client secrets, UUIDs, names, or dates retained.

## Decision this ticket produces

Record one of the following in `ENDPOINT_MATRIX.md`, replacing the pending row:

- **Bound** — patient-context writes are enforced. Re-probe TICK-016 under a patient
  token and drop the staff-credential path.
- **Unbound** — a patient token can write another chart. Mark the route permanently
  rejected, record it as an upstream security finding, and the product performs no
  demographic write on v8.3.0.
- **Unavailable** — no patient-context token is obtainable at all on v8.3.0. The
  product's authorization premise does not hold and the scope must change.

## Testing

Run against the pinned local stack in `deploy/local/`. The probe script must be
committed and re-runnable from a clean stack. CI must be green.

## Out of Scope

Production authorization, the portal iframe hook (TICK-002), and any remediation of
whatever this spike finds.
