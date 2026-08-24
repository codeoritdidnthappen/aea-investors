# TICK-065 — live verification with the model server stopped

Reproduce: `sh evidence/TICK-065/run_live_verification.sh`
Raw transcript: [`run-live-verification.txt`](run-live-verification.txt)
Date: 2026-08-23

This closes the IOU at `evidence/TICK-063/VERIFICATION_2026-08-23.md:106-108`.

## What is pinned

| Thing | Value |
|---|---|
| Worktree base commit | `09ac816` (TICK-064) |
| AI server | this worktree, `uv run uvicorn ai_server.app.main:app`, port 8765 |
| Session database | a throwaway `mktemp -d` SQLite file, deleted on exit |
| "Stopped" model server | `OLLAMA_HOST=http://127.0.0.1:11498`, confirmed refused before the run |
| Recovered model server | `ollama/ollama:0.32.15`, container `tick065-ollama`, port 11497 |
| OpenEMR | the running `local-openemr-1` / `local-mariadb-1` / `local-caddy-1` stack |
| Patient record checked | `openemr.patient_data`, all three seeded patients |

The AI server runs on its own port with its own database, and the model server is stopped
by pointing at a port nothing is listening on — which is precisely what a stopped
container looks like to this code: a refused TCP connection, surfacing as
`LocalModelUnavailableError`. Nothing about the running `deploy/local` stack was
modified, restarted, or otherwise disturbed.

## Claims

| # | Claim | AC | Result |
|---|---|---|---|
| 1 | `/health` reports `model_server: unavailable` during the outage | AC4 | **pass** — step 2 |
| 2 | `/health` reports `model_server: ok` once a real model server is listening, so the probe observes rather than being unconditionally pessimistic | AC4 | **pass** — step 7 |
| 3 | An ordinary turn is answered with the honest unavailable message | AC3 | **pass** — step 4 |
| 4 | A turn that would *write* is answered the same way, not differently and not with a partial attempt | AC3, AC5 | **pass** — step 4 |
| 5 | The patient's address in OpenEMR's own database is byte-identical before and after | AC5 | **pass** — steps 3 and 5 |
| 6 | The OpenEMR patient portal keeps serving throughout | AC3 | **pass** — step 6 |

### Claim 5 in detail

This is the claim the ticket actually rests on, so it is checked at the record rather than
at the reply. Before and after the write-intent turn:

```
1|2002 Bridge Avenue|Sulfur|LA|70663
2||||
3||||
```

Patient 1's street is worth pausing on. `2002 Bridge Avenue` is not a seed value — it is
the artefact of the bug `docs/LOCAL_LLM_SPEC.md` opens with, where
`address_chat._parse_freeform_address` assigned the first comma-separated segment of
`"Update it to: 2002 Bridge Avenue"` to `street1` unexamined and wrote it to the chart.
The function that produced it was deleted by this ticket. The row it corrupted is still
there, and served here as the control: an outage-time write would have moved it.

### Claim 6 in detail

```
portal login page: HTTP 200
portal home:       HTTP 302
```

`302` on `portal/home.php` is the correct answer to an unauthenticated request — OpenEMR
redirecting to its own login — not a failure. The portal is answering normally with the
assistant down, which is what the unavailable message tells the patient.

## The message the patient saw

> The AI assistant is temporarily unavailable, so it could not handle your request. Your
> patient portal is still working normally. Please try again in a little while, or
> contact the clinic directly. To book or change an appointment in the meantime, use the
> appointment scheduling option in your OpenEMR portal menu.

Against AC3: it says *temporarily* unavailable; it says the portal still works in as many
words rather than leaving it to be inferred; it names no internal component (not the
model server, not the provider, not the AI server); and the one next step it offers is
OpenEMR's own patient-portal scheduling screen, which is not a degraded path through this
system — nothing that is currently down is involved in it.

## Rough edges, recorded rather than smoothed over

- **`openemr_api` also reports `unavailable` in this transcript.** That is an artefact of
  where the verification server runs, not a finding. `OPENEMR_OAUTH_ISSUER` is set to
  `https://openemr`, the compose service name, which resolves inside the app network and
  not from the host. The same is true of `ocr` (no local Tesseract on the host) and
  `external_llm` (no `GROQ_API_KEY` exported). Only the `model_server` line varies with
  the thing under test, and it is the line that changes between steps 2 and 7.
- **The session is minted directly** (`mint_session.py`) rather than through the OAuth
  round trip, which needs a browser. The row is the production shape, written by the same
  `SessionStore.create_session` the callback calls, and the server reads it through its
  ordinary `active_session` check. Nothing on the outage path is faked by this.
- **Step 7 restarts the AI server** rather than re-reading configuration, because
  `HealthSettings.from_environment` is read once at startup by design. So claim 2 is
  "a deployment pointed at a live model server reports `ok`", not "a running process
  notices a server coming back". The latter is true too — the probe runs per request —
  but this transcript does not show it.
- **The corrupted `2002 Bridge Avenue` row was left alone.** Cleaning it up is not this
  ticket's business, and it is more useful as a control than as a tidy record.

## Not verified here

- The containerised `deploy/local` topology with `LLM_PROVIDER=ollama`. This ran the AI
  server from the worktree on the host. The container path is exercised by
  `deploy/local/verify-stack.sh` and unchanged by this ticket, except that `/health` now
  carries one more dependency name.
- Behaviour of a model server that is *listening but sick* — answering `/v1/models` while
  failing completions. The probe would report `ok` and turns would answer unavailable.
  That is a real gap, deliberately accepted: a probe that ran an inference on every
  monitoring poll would put load on the model server and would read a slow generation as
  an outage.
- The chat page's client-side fallback panel, which is a browser behaviour. It is covered
  by `test_chat.py`'s assertions on `CHAT_PAGE_HTML` and was not exercised with a real
  browser here.
