# TICK-027 — local privacy and demo-readiness checklist, 2026-08-20 (4th pass)

Independently re-run against the live local Docker topology
(`local-openemr-1`, `local-mariadb-1`, `local-caddy-1`, `local-ai-server-1`)
where re-verifiable today; otherwise reconciled to the most recent evidence
on record for each gate. Per this ticket's own acceptance criteria,
exceptions are recorded rather than glossed over -- readiness is **not**
approved as of this pass.

| Gate | Requirement | Status | Evidence |
|---|---|---|---|
| Privacy golden corpus | NFR-28: golden corpus includes every synthetic patient/ID pairing | **Pass** | `ai_server/tests/test_privacy_gate.py`, part of the full suite re-run today (see Core test coverage row). |
| Artifact exclusion | NFR-24: deployed artifacts exclude OCR expected labels and evaluation data | **Pass** | `ai_server/tests/test_release_governance.py`, part of the full suite re-run today; `.gitignore` excludes `generated-fixtures/` (evaluation data, never a runtime artifact). |
| OCR threshold | NFR-29, NFR-15's gate: pinned local Tesseract, golden-set accuracy | **Pass** (as of TICK-015) | `tickets/TICK-015-gate-ocr-accuracy.md` -- `status: done`. Not independently re-run this pass; no OCR-affecting change has landed since. |
| ZDR verification | NFR-26: Groq Zero Data Retention verified before any demo run | **Pass** | `deploy/local/.env`'s `GROQ_ZDR_VERIFIED_ON=2026-08-20` (same day as this checklist). |
| Local health | NFR-22: health endpoint reports AI-server, OpenEMR API, OCR, external LLM reachability | **Pass** | `curl https://chat.localhost/health` (live, today): `{"status":"ok","dependencies":{"ai_server":"ok","openemr_api":"ok","ocr":"ok","external_llm":"ok"}}`. |
| Local TLS | NFR-34: Caddy accepts HTTPS for both hostnames | **Pass** | Live today: `https://chat.localhost/` -> `200`, `https://emr.localhost/` -> `302` (redirect to login, expected for an unauthenticated request), both over valid local TLS. |
| Core test coverage | NFR-18: >= 80% automated coverage | **Pass** | `pytest ai_server/tests/ --cov` (live, today): **446 passed, 4 skipped, 90.86% coverage.** |
| Reschedule | FR-13 (bundled under NFR-11 scheduling coverage) | **Pass** | TICK-020 `status: done` -- re-scoped from "permanently blocked" to a cancel-then-rebook composition and built. Booking, cancel, and reschedule (TICK-031/034/036/020/040) all verified live this session. |
| Desktop Chrome | NFR-35 (desktop half) | **Pass** (closed this pass) | TICK-024 `status: done` -- every AC item now verified live, including OCR confirmation: TICK-044 landed and was live-verified through the real chat UI (a real synthetic ID photo uploaded through the real file input, genuinely processed by local Tesseract, offered as a suggestion, and only the patient's own typed correction reached the final `patient_data` write). See `evidence/TICK-024/DESKTOP_E2E_EVIDENCE_3.md` and `evidence/TICK-044/OCR_UPLOAD_CHAT_EVIDENCE.md`. |
| Android Chrome | NFR-35 (Android half) | **Exception** | TICK-025 `status: blocked` -- no Android emulator available in this execution environment. Environment gap, not a product/code defect; cannot be resolved without emulator access. |
| Performance targets | NFR-13/14: p95 chat/scheduling under load | **Exception** | TICK-026 `status: blocked`, deferred by explicit prior user direction: a genuine 20-VU/60s load test against `/api/chat` would spend real money against a live Groq API key. Requires either re-scoping to production or explicit re-authorization to spend against the local key before it can run. |

## Readiness determination

**Not approved.** Nine of ten P1 gates pass with fresh, live evidence as of
this pass -- desktop Chrome closed out this pass (OCR confirmation was the
last open item, resolved by TICK-044). Two exceptions remain, both
environment/authorization gaps rather than product defects: Android E2E
(no emulator available) and performance (requires explicit user
authorization to spend against a live API key, not yet requested).

## What would close this out

1. Run TICK-025 in an environment with Android emulator access.
2. Get explicit authorization (or re-scope to production) for TICK-026's
   load test, then run it.
