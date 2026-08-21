# TICK-027 — local privacy and demo-readiness checklist, 2026-08-21 (6th pass, closed)

Independently re-run against the live local Docker topology
(`local-openemr-1`, `local-mariadb-1`, `local-caddy-1`, `local-ai-server-1`)
where re-verifiable today; otherwise reconciled to the most recent evidence
on record for each gate.

**Scope note (2026-08-21):** per explicit user direction, Android Chrome
(NFR-35's Android half) is excluded from this ticket's own closing scope
and tracked instead as TICK-025's own separate ticket -- there is no
Android emulator or device reachable from this environment, and no code
change can substitute for one. It is recorded below for completeness, not
silently dropped.

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
| Performance targets | NFR-13/14: p95 chat/scheduling under load | **Pass** (re-scoped) | TICK-026 `status: done` -- closed at an explicitly authorized, deliberately reduced scale (5 VU/30s, not the NFR-13/NFR-14-documented 20 VU/60s) per explicit user direction 2026-08-21. All three measured p95s passed with wide margin: chat/API 409ms (<3.0s target), scheduling read 29.5ms and write 52.94ms (<1.0s target). See `evidence/TICK-026/PERFORMANCE_TRIAL_2026-08-21.md`. |
| Android Chrome | NFR-35 (Android half) | **Out of scope for this ticket** (2026-08-21) | TICK-025 `status: blocked` -- no Android emulator or device reachable from this environment; no code change substitutes for one. Per explicit user direction, excluded from TICK-027's own closing scope rather than left as an indefinite blocker here. Tracked as its own resumable ticket for whenever emulator/device access exists -- not a product/code defect, and nothing here indicates Android Chrome itself is broken. |

## Readiness determination

**Approved for desktop-only local-demo readiness, Android explicitly out of
scope.** Every gate within this ticket's re-scoped closing criteria passes,
seven at their originally-documented scale and one (performance) at an
explicitly authorized reduced scale -- see that gate's own note above.
Android Chrome (NFR-35's mobile half) is a real, tracked, unresolved gap --
not silently dropped -- but is excluded from this ticket's own scope per
explicit user direction and lives on as TICK-025.

## What would close TICK-025 (tracked separately, not blocking this ticket)

Run it in an environment with real Android emulator or device access (local
Android Studio emulator, physical device over ADB, or a remote device
farm). Update AND-SCHEDULE-01's reschedule step to book/cancel-only first
(TICK-020 permanently blocks reschedule regardless of platform).

## If full-scale performance evidence is needed later

`scripts/k6_performance_test.js` and `scripts/load_test_chat_browser.js`
already support the originally-documented 20-VU/60s scale unchanged --
re-run with `VUS=20`/`DURATION=60s` (e.g. before a production release
claim).
