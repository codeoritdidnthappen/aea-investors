# TICK-059 — Ollama in the local topology, model pinned

**Date:** 2026-08-23
**Host:** Apple Silicon Mac, Docker Desktop 29.6.2 (Linux VM, **no GPU passthrough**)
**Reproduce:** `./run_live_verification.sh <warm-model-volume>` in this directory.

Every number below comes from that script. It stands up the two images the compose
file builds, on a throwaway network, with the model server named `ollama` exactly as
Compose names it. It deliberately does **not** run `docker compose up` from the build
worktree — the preflight refuses that and TICK-057 is why.

## What is pinned

| | Value |
|---|---|
| Model server image | `ollama/ollama:0.32.15` (exact patch release, not `latest`, not an `-rc`) |
| Model | `qwen2.5:7b-instruct-q4_K_M` — 7B, GGUF Q4_K_M (LOCAL_LLM_SPEC D11) |
| Model digest | `sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e` |

The model is **provisional**: TICK-062 benchmarks the D11 class and picks the final
one. This ticket's job was to make the model a pinned, swappable setting, which it
is — `LLM_MODEL` / `LLM_MODEL_DIGEST`, defaulted in the committed compose file and
overridable from `.env`.

### Why that digest is the right thing to pin

Ollama writes the registry manifest to disk verbatim, so the sha256 of that file is
the model's identity. Confirmed three independent ways before it was used as the pin:

```
registry manifest body, fetched over HTTPS   845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e
on-disk manifest after `ollama pull`         845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e
GET /api/tags -> .models[].digest            (same, verified on the 0.5b model)
`ollama list` ID column                      845dbda0ea48  (first 12 characters)
```

It covers the weights *and* the template and parameters, so a retuned prompt
template shipped under the same tag is caught too, not just requantised weights.

## Results

| # | Claim | Result |
|---|---|---|
| 1 | Cold volume pulls the model with no manual step | **pass** — 39s for the 0.5b stand-in, digest verified, then serving |
| 2 | Warm volume verifies the pinned 7B and serves | **pass** — ready in <1s |
| 3 | AI server reaches it **by service name** and gets a completion | **pass** — `endpoint=http://ollama:11434/v1/chat/completions` |
| 4 | Recreate reuses the weights, re-downloads nothing | **pass** — blobs byte-identical, no pull, 0s |
| 5a | A wrong digest refuses to serve | **pass** — exit code 1, container not running |
| 5b | Healthcheck fails when the model is absent | **pass** |
| 5c | Healthcheck passes on a healthy warm server | **pass** |
| 6 | Runs with no GPU | **pass** — `library=cpu`, no `/dev/nvidia*`, no `/dev/dri` |

### 3 — reachability, through the real client

The probe goes through `ai_server.llm.local.LocalModelSettings.from_environment()`
and `HttpLocalModelClient`, not a hand-rolled request. A `curl` would prove the
container resolves the name; it would not prove the *configured* client does, and
that client's built-in fallback is `http://localhost:11434` — which inside the
ai-server container is the AI server itself. That fallback is the exact failure this
criterion exists to rule out, so the probe fails loudly if `base_url` is a host
address.

```
model=qwen2.5:7b-instruct-q4_K_M
endpoint=http://ollama:11434/v1/chat/completions
PROBE_OK
```

### 6 — no GPU, from the model server's own logs

```
ls: cannot access '/dev/nvidia*': No such file or directory
ls: cannot access '/dev/dri': No such file or directory
msg="inference compute" id=cpu library=cpu name=cpu description=cpu total="7.7 GiB" available="7.7 GiB"
```

Docker Desktop on macOS passes no accelerator through, so this run *is* the no-GPU
case. The compose file declares no `deploy.resources.reservations.devices` block,
which would have hard-failed `docker compose up` on exactly this machine.

## Timings — the first real data on D11's size assumption

| Measurement | Value |
|---|---|
| Cold pull, `qwen2.5:7b-instruct-q4_K_M` (4.7 GB) | **460s** (7m40s) at ~10 MB/s |
| Cold pull, `qwen2.5:0.5b-instruct-q4_K_M` (398 MB) | **39s** including verify + serve |
| Warm start (model already in the volume) | **<1s** to healthy |
| Recreate | **0s** to healthy, 0 bytes re-downloaded |
| `complete()` — structured JSON plan, cold model load | 4.8s / 6.3s (two runs) |
| `stream()` — first token, model already resident | **0.07s / 0.12s** |
| `stream()` — full response | 0.8s–2.6s (97 chunks / 223 chars) |

**Read these as a floor, not a forecast.** They are CPU-only inside Docker
Desktop's Linux VM with 7.7 GiB visible. Native Apple Silicon with Metal would be
materially faster, and OCI ARM cores (D5) materially different again. What they do
establish: a 7B Q4_K_M model is *comfortable* on this hardware even without a GPU —
sub-second to first token once resident, seconds for a full structured plan. D11's
size assumption holds on the development machine. It says nothing about deployment.

The 4.8–6.3s `complete()` figures include loading the model into memory on the first
call; the sub-second streaming numbers are the steady state and are the ones D16's
"visible pause before the first token" should be judged against.

### An honest quality observation

The two runs returned different structured output for the same prompt:

```
run 1  {"intent": "information", "appointment_token": "appt_tick059probe-upcomng-schdlng-info-cnfrmtn-2026-08-24T0400Z-0500-"}
run 2  {"intent": "information", "appointment_token": "appt_tick059probe"}
```

Run 1's token is fabricated — it does not match any token in the payload. This is
LOCAL_LLM_SPEC D11's "at this size complex multi-turn judgement is uneven" showing up
on the very first live call, and it is precisely why D6 (`model proposes, code
disposes`) and D15 (zero wrong writes) exist. It is **not** a defect in this ticket —
nothing here routes a turn through the model — but it is data TICK-062's benchmark and
TICK-061's validator work should have.

## The guards discriminate

A check that cannot fail proves nothing, so each was shown failing on a broken input
and passing on a good one.

**Digest pin** — the shipped model, with the pinned digest replaced by 64 zeros:

```
aeai-ollama: PINNED MODEL DIGEST MISMATCH -- refusing to serve
aeai-ollama:   expected sha256:0000000000000000000000000000000000000000000000000000000000000000
aeai-ollama:   actual   sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e
exit_code=1   running=false
```

It refuses to *serve*, rather than serving with a warning. A model server answering
with unreviewed weights is worse than one that is down: D12 already makes an
unavailable model an honest, visible outage.

**Healthcheck** — same image, same pin, volume with no model:

```
pinned model qwen2.5:7b-instruct-q4_K_M is not in the volume (expected /root/.ollama/...)
HEALTHCHECK_CORRECTLY_FAILED
```

and on the warm volume, `HEALTHCHECK_CORRECTLY_PASSED`.

**`verify-stack.sh`'s new section** — executed, not merely written. The block was
extracted verbatim from the committed file with `sed` and run against a two-service
Compose project (`ollama` + `ai-server`, the same two images, the 0.5b model to keep
it quick). All four branches were driven:

| Scenario | Output | Exit |
|---|---|---|
| Model server stopped | `the model server (ollama) is not running` | 1 |
| Everything healthy | `VERIFY_STACK_OK` | 0 |
| `OLLAMA_HOST=http://localhost:11434` | `the ai-server cannot reach the model server at http://localhost:11434` | 1 |
| `OLLAMA_HOST` empty | `the ai-server container has no OLLAMA_HOST set` | 1 |

The third row is the important one: that is the client's own built-in fallback, and
the check catches it.

> **A bug this found in the check itself.** The running-container guard was first
> written as `if ! docker compose ps --status running ollama`, copying the existing
> `openemr` guard in the same file. Live, with the model server **stopped**, that
> command still **exits 0** — so the guard never fired, and the failure was only
> reported a few lines later by the healthcheck exec. A guard that cannot fail, in
> the file whose entire purpose is catching silent failure. It now uses `--quiet` and
> tests for empty output. `test_verify_stack_tests_for_a_running_container_not_for_an_exit_status`
> pins this so it cannot be reverted.
>
> The pre-existing `openemr` guard at the top of the file has the same weakness. It
> is left alone deliberately — it is TICK-057's code, not this ticket's, and its
> practical effect there is also only a less specific message rather than a missed
> failure. Worth a follow-up ticket.

**Preflight** — run against a copy of the shipped compose file (copied out of the
worktree, since the preflight refuses worktree paths):

```
A. shipped compose, unmodified              PREFLIGHT_OK                    exit=0
B. tag floated to qwen2.5:latest            "not pinned to a specific tag"  exit=1
C. LLM_MODEL_DIGEST line deleted            "has no pinned digest"          exit=1
```

`docker compose config` resolves the shipped file cleanly (`COMPOSE_CONFIG_VALID`),
with `LLM_MODEL: qwen2.5:7b-instruct-q4_K_M`, the full digest, and
`OLLAMA_HOST: http://ollama:11434`.

## Not verified here

- **The full stack.** `openemr`, `mariadb` and `caddy` were not started. This ran
  the two images the ticket touches, on their own network. The compose wiring that
  joins them was verified statically (`docker compose config`, and the tests in
  `ai_server/tests/test_local_deployment.py`), not by a full `docker compose up`.
  A full-stack run must happen from the main checkout, never a worktree.
- **`verify-stack.sh` as a whole was not run**, only its TICK-059 block (see above,
  where all four of its branches were driven against a real two-service project).
  The file's earlier `openemr`/`mariadb` checks need the full stack, and they exit
  before the new section is reached — which is why the block had to be extracted to
  exercise it at all.
- **Routing a real chat turn through the model.** `LLM_PROVIDER` still defaults to
  `groq`; changing how turns are routed is explicitly out of this ticket's scope
  (TICK-061/TICK-066). The probe exercises the transport, not the chat.
- **Native Apple Silicon / Metal performance.** Every timing is CPU-only inside
  Docker Desktop's VM. See the warning above.
- **OCI / vLLM (D5, TICK-067).** Untouched.
- **The model choice itself.** TICK-062. The digest pinned here is a real, verified
  pin of a real model in D11's class, not a placeholder string — but the model it
  points at is provisional.
