# TICK-027 — local privacy and demo-readiness checklist, 2026-08-21 (5th pass)

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
| Desktop Chrome | NFR-35 (desktop half) | **Pass** | TICK-024 `status: done` -- every AC item verified live, including OCR confirmation (TICK-044). See `evidence/TICK-024/DESKTOP_E2E_EVIDENCE_3.md` and `evidence/TICK-044/OCR_UPLOAD_CHAT_EVIDENCE.md`. |
| Performance targets | NFR-13/14: p95 chat/scheduling under load | **Pass** (re-scoped, closed this pass) | TICK-026 `status: done` -- closed at an explicitly authorized, deliberately reduced scale (5 VU/30s, not the NFR-13/NFR-14-documented 20 VU/60s) per explicit user direction 2026-08-21. All three measured p95s passed with wide margin: chat/API 409ms (<3.0s target), scheduling read 29.5ms and write 52.94ms (<1.0s target). See `evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md`. The full 20-VU/60s scale itself was not run; `scripts/k6_performance_test.js`/`scripts/load_test_chat_browser.js` support it unchanged if needed later (e.g. before a production release claim). |
| Android Chrome | NFR-35 (Android half) | **Exception** | TICK-025 `status: blocked` -- no Android emulator available in this execution environment. Environment gap, not a product/code defect; cannot be resolved without emulator access. |

## Readiness determination

**Not approved.** Nine of ten gates now pass, eight with evidence at their
originally-documented scale and one (performance) closed at an explicitly
authorized reduced scale per direct user direction rather than the
originally-documented 20-VU/60s figure -- see that gate's own note above.
One exception remains, an environment gap rather than a product defect:
Android E2E has no emulator available in this execution environment.

## What would close this out

1. Run TICK-025 in an environment with Android emulator access.
2. If a production release claim later requires performance evidence at the
   full, originally-documented 20-VU/60s scale (rather than the
   accepted 5-VU/30s figure), re-run `scripts/k6_performance_test.js` and
   `scripts/load_test_chat_browser.js` with `VUS=20`/`DURATION=60s`.
