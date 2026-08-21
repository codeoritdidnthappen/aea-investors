# TICK-026: performance trial, 2026-08-21

## Scope of this run (explicit, user-authorized)

Prior direction deferred this ticket entirely, citing real cost against a
live Groq API key at the ticket's own documented 20-VU/60s scale. Asked
explicitly before running anything; user authorized a **smaller, cost-contained
trial (5 VUs, 30 seconds)** covering both halves together (chat/API and
OpenEMR scheduling reads/writes), not the full 20-VU/60s scenario AC1
originally named.

**Update, 2026-08-21:** after reviewing these results, the user explicitly
directed that this reduced scale be accepted as TICK-026's own completed
scenario rather than left open pending the full 20/60 figure. The ticket is
closed (`status: done`) on that basis -- see its own "Scope note" for the
same decision recorded there. `PRD.md`'s NFR-13/NFR-14 text (which still
documents 20 VU/60s) was intentionally left unchanged; only this ticket's
own AC1 was re-scoped.

## Credential provisioning

A genuine OAuth+PKCE login as the synthetic patient `AverySubjecttest1`,
consented live through the real browser (headless replication of the
consent form was attempted first and failed -- the granted scope came back
as just `"nonce"`, missing `id_token`/`refresh_token` entirely, most likely
because the consent page's per-resource checkboxes are packaged into the
submission by page JS a raw POST doesn't replicate; a real browser click
resolved it immediately). Produced:

- A raw bearer `access_token` (full scope grant, including
  `patient/appointment.c`/`.u`, `patient/demographics.u`, etc.) captured by
  intercepting the authorization `code` mid-redirect and exchanging it
  directly against `/oauth2/default/token` with a self-generated PKCE
  verifier -- used for the OpenEMR scheduling scenario.
- A genuine `ai_session` cookie via the ai-server's own `/oauth/launch` ->
  `/oauth/callback` flow (HttpOnly by design, matching this project's own
  "raw token never reaches the browser" posture) -- used for the chat/API
  scenario by running that scenario's load generator *inside* the
  authenticated browser tab instead of an external k6 process (see
  `scripts/load_test_chat_browser.js`).

Neither credential was committed, logged in full, or retained past this
run.

## Results (all real, live requests against the local Docker topology)

| Scenario | Target (NFR) | p95 (5 VU / 30s trial) | Result |
|---|---|---|---|
| Chat/API (`POST /api/chat`, real Groq calls) | <3.0s (NFR-13) | **409ms** | Pass |
| OpenEMR scheduling read (`GET .../fhir/Appointment`) | <1.0s (NFR-14) | **29.5ms** | Pass |
| OpenEMR scheduling write (book + cancel, `POST`/`PUT .../portal/patient/appointment`) | <1.0s (NFR-14) | **52.94ms** | Pass |

All three comfortably clear their documented target at this trial's scale.

### Chat/API detail

533 successful requests in 30.5s (real, logged server-side: `docker logs
local-ai-server-1` shows 5 concurrent keep-alive connections issuing real
`POST /api/chat` calls, each genuinely reaching Groq -- confirmed several
hundred real, metered LLM calls were made, as originally flagged). p50
299ms, p95 409ms, p99 633ms, max 1.03s. Zero errors.

### Scheduling detail, and a real methodology finding

The first combined run (read + write scenarios concurrently, both against
the same synthetic patient) measured scheduling-read p95 at **1.68s** --
*failing* the 1.0s target. Investigated rather than reported at face value:
the concurrent write scenario had created 1,771 appointments for that one
patient within the 30-second window (each book-then-cancel iteration adding
one row, all showing up in every subsequent read's FHIR bundle), so the read
scenario was serializing an ever-growing bundle over the course of the same
run -- a self-inflicted test-design confound (two scenarios sharing one
patient's rapidly-growing history), not a genuine OpenEMR read-performance
problem. Cleaned up the 1,771 synthetic rows and re-ran the read scenario in
isolation: **p95 29.5ms**, cleanly passing. The table above reports the
corrected, unconfounded numbers.

**Lesson for the full run**: scheduling-read and scheduling-write should
either use separate patients, or the read scenario should run before the
write scenario populates extra data, to avoid this same confound recurring
at 20-VU/60s scale (where it would compound faster and produce an even more
misleading read-latency number).

## What this does and doesn't prove

Proves the local deployment is fast and healthy at 1/4 the documented
scale, with two real reproducible scripts now checked in
(`scripts/k6_performance_test.js` for scheduling,
`scripts/load_test_chat_browser.js` for chat/API) ready to run at the full
20-VU/60s scale later if needed. Per explicit user direction (see "Update,
2026-08-21" above), this trial's own reduced scale is what TICK-026's AC1
is closed against -- it does not itself demonstrate performance at the
originally-documented 20-VU/60s figure, which remains a separate,
not-yet-run scenario the same scripts already support unchanged.

## Reproduction

```
k6 run scripts/k6_performance_test.js \
  -e VUS=5 -e DURATION=30s \
  -e EMR_BASE_URL=https://emr.localhost -e ACCESS_TOKEN=<bearer token>
```

For chat/API: paste `scripts/load_test_chat_browser.js` into DevTools
Console on an authenticated `https://chat.localhost/` tab.
