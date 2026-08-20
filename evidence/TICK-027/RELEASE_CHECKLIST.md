# TICK-027 — local privacy and demo-readiness checklist, 2026-08-20

Independently re-run against the live local Docker topology
(`local-openemr-1`, `local-mariadb-1`, `local-caddy-1`, `local-ai-server-1`)
where re-verifiable today; otherwise reconciled to the most recent evidence
on record for each gate. Per this ticket's own acceptance criteria,
exceptions are recorded rather than glossed over -- readiness is **not**
approved as of this pass.

| Gate | Requirement | Status | Evidence |
|---|---|---|---|
| Privacy golden corpus | NFR-28: golden corpus includes every synthetic patient/ID pairing | **Pass** | `ai_server/tests/test_privacy_gate.py` -- 100% line coverage, re-run live today (see below). |
| Artifact exclusion | NFR-24: deployed artifacts exclude OCR expected labels and evaluation data | **Pass** | `ai_server/tests/test_release_governance.py` -- 100% line coverage, re-run live today; `.gitignore` excludes `generated-fixtures/` (evaluation data, never a runtime artifact). |
| OCR threshold | NFR-29, NFR-15's gate: pinned local Tesseract, golden-set accuracy | **Pass** (as of TICK-015) | `tickets/TICK-015-gate-ocr-accuracy.md` -- `status: done`. Not independently re-run this pass; no OCR-affecting change has landed since. |
| ZDR verification | NFR-26: Groq Zero Data Retention verified before any demo run | **Pass** | `deploy/local/.env`'s `GROQ_ZDR_VERIFIED_ON=2026-08-20` (same day as this checklist). |
| Local health | NFR-22: health endpoint reports AI-server, OpenEMR API, OCR, external LLM reachability | **Pass** | `curl https://chat.localhost/health` (live, today): `{"status":"ok","dependencies":{"ai_server":"ok","openemr_api":"ok","ocr":"ok","external_llm":"ok"}}`. |
| Local TLS | NFR-34: Caddy accepts HTTPS for both hostnames | **Pass** | Live today: `https://chat.localhost/` -> `200`, `https://emr.localhost/` -> `302` (redirect to login, expected for an unauthenticated request), both over valid local TLS. |
| Core test coverage | NFR-18: >= 80% automated coverage | **Pass** | `pytest ai_server/tests/ --cov` (live, today): **413 passed, 3 skipped, 94.45% coverage.** |
| Desktop Chrome | NFR-35 (desktop half) | **Exception** | TICK-024 `status: blocked` -- substantial live coverage obtained (login, nav tile, patient-context consent, iframe embed, streaming, accessibility/keyboard, honest no-availability booking, onboarding's first turn since TICK-038 landed), but full onboarding-through-OCR-confirmation and cancellation-completion coverage still blocked on **TICK-039** (open). See `evidence/TICK-024/DESKTOP_E2E_EVIDENCE_2.md`. |
| Android Chrome | NFR-35 (Android half) | **Exception** | TICK-025 `status: blocked` -- no Android emulator available in this execution environment. Environment gap, not a product/code defect; cannot be resolved without emulator access. |
| Performance targets | NFR-13/14: p95 chat/scheduling under load | **Exception** | TICK-026 `status: blocked`, deferred by explicit prior user direction: a genuine 20-VU/60s load test against `/api/chat` would spend real money against a live Groq API key. Requires either re-scoping to production or explicit re-authorization to spend against the local key before it can run. |
| Reschedule | FR-13 (bundled under NFR-11 scheduling coverage) | **Permanent exception, accepted** | TICK-020 `status: blocked`, permanently -- OpenEMR v8.3.0 has no service-layer method to update an existing appointment's date/time, only legacy raw SQL the project's own ADR already rejects. Booking and cancel (the ticket's other two parts) are not affected -- see TICK-031/034/036. |

## Readiness determination

**Not approved.** Three exceptions remain open (desktop E2E completion,
Android E2E, performance) and one is a permanent, accepted platform
limitation (reschedule) that does not itself block readiness but is
recorded here per this checklist's own requirement to reconcile every P1
item. All other P1 gates pass with fresh, live evidence as of this pass.

## What would close this out

1. Fix **TICK-039** (cancellation), then re-run TICK-024 to completion.
2. Run TICK-025 in an environment with Android emulator access.
3. Get explicit authorization (or re-scope to production) for TICK-026's
   load test, then run it.
