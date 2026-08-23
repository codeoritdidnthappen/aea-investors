#!/usr/bin/env sh
# TICK-059 live verification. Reproduces every timing and every pass/fail claim in
# VERIFICATION_2026-08-23.md.
#
# This does NOT run `docker compose up` from a build worktree -- the preflight
# refuses that, and TICK-057 is why. Instead it stands up the same two images the
# compose file builds, on a throwaway user-defined network, with the model server
# named `ollama` exactly as Compose would name it. Service-name resolution, the
# digest pin, the healthcheck and the persistence claim are all properties of the
# images and the named volume, so they are provable this way; what is not provable
# this way is listed under "Not verified here" in the write-up.
#
# Usage: ./run_live_verification.sh [warm-model-volume]
# Everything it creates is named tick059-* and is removed at the end.
set -eu

MODEL=qwen2.5:7b-instruct-q4_K_M
DIGEST=sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e
SMALL_MODEL=qwen2.5:0.5b-instruct-q4_K_M
SMALL_DIGEST=sha256:a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67

NET=tick059-verify
WARM_VOLUME=${1:-tick059-warm-models}
COLD_VOLUME=tick059-cold-models
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)

step() { printf '\n=== %s\n' "$1"; }

cleanup() {
    docker rm -f tick059-ollama tick059-probe >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker volume rm "$COLD_VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

step "Building the two images from the committed Dockerfiles"
docker build -q -f "$REPO_ROOT/deploy/local/ollama.Dockerfile" -t tick059-ollama:verify "$REPO_ROOT"
docker build -q -f "$REPO_ROOT/deploy/local/ai-server.Dockerfile" -t tick059-aiserver:verify "$REPO_ROOT"

docker network create "$NET" >/dev/null 2>&1 || true

start_model_server() {
    docker rm -f tick059-ollama >/dev/null 2>&1 || true
    docker run -d --name tick059-ollama --network "$NET" --network-alias ollama \
        -e LLM_MODEL="$1" -e LLM_MODEL_DIGEST="$2" \
        -v "$3:/root/.ollama" tick059-ollama:verify >/dev/null
}

wait_healthy() {
    waited=0
    until docker exec tick059-ollama /usr/local/bin/aeai-ollama-healthcheck >/dev/null 2>&1; do
        if ! docker inspect -f '{{.State.Running}}' tick059-ollama 2>/dev/null | grep -q true; then
            echo "model server exited before becoming ready" >&2
            docker logs tick059-ollama 2>&1 | tail -20 >&2
            return 1
        fi
        [ "$waited" -ge "$1" ] && { echo "not ready within $1s" >&2; return 1; }
        sleep 2
        waited=$((waited + 2))
    done
    echo "ready after ${waited}s"
}

# --- 1. Cold volume: the model arrives with no manual pull step (AC3) ----------
step "1. Cold volume auto-pull (small model, to keep this reproducible in minutes)"
docker volume rm "$COLD_VOLUME" >/dev/null 2>&1 || true
cold_start=$(date +%s)
start_model_server "$SMALL_MODEL" "$SMALL_DIGEST" "$COLD_VOLUME"
wait_healthy 600
cold_end=$(date +%s)
echo "cold_start_seconds=$((cold_end - cold_start))"
docker logs tick059-ollama 2>&1 | grep -E 'aeai-ollama:' | head -5
docker rm -f tick059-ollama >/dev/null

# --- 2. Warm volume: the pinned 7B model, verified and served -----------------
step "2. Warm volume, pinned model $MODEL"
warm_start=$(date +%s)
start_model_server "$MODEL" "$DIGEST" "$WARM_VOLUME"
wait_healthy 300
warm_end=$(date +%s)
echo "warm_start_seconds=$((warm_end - warm_start))"
docker logs tick059-ollama 2>&1 | grep -E 'aeai-ollama:' | head -5

step "2b. The digest the server verified, as the API reports it"
docker exec tick059-ollama sh -c \
    'sha256sum /root/.ollama/models/manifests/registry.ollama.ai/library/qwen2.5/7b-instruct-q4_K_M'

# --- 3. The AI server reaches it by service name and gets a completion (AC1) ---
step "3. AI server -> model server, by service name, through the real client"
docker run --rm --name tick059-probe --network "$NET" \
    -e LLM_PROVIDER=ollama \
    -e LLM_MODEL="$MODEL" \
    -e OLLAMA_HOST=http://ollama:11434 \
    -v "$REPO_ROOT/evidence/TICK-059/probe_model_server.py:/app/probe.py:ro" \
    tick059-aiserver:verify python /app/probe.py

# --- 4. A recreate reuses the volume and re-downloads nothing (AC4) ------------
step "4. Recreate on the same volume"
# Measured from inside the model server itself rather than with a helper image, so
# the numbers describe the volume exactly as the process that reads it sees them.
#
# `models/blobs` specifically, not all of `/root/.ollama`: the server writes a few
# KB of its own runtime state (a keypair, history) on every start, so comparing the
# whole directory reports a difference that has nothing to do with the weights.
# The blobs are the several GB this criterion is about.
before=$(docker exec tick059-ollama du -sk /root/.ollama/models/blobs | cut -f1)
recreate_start=$(date +%s)
start_model_server "$MODEL" "$DIGEST" "$WARM_VOLUME"
wait_healthy 300
recreate_end=$(date +%s)
after=$(docker exec tick059-ollama du -sk /root/.ollama/models/blobs | cut -f1)
echo "recreate_seconds=$((recreate_end - recreate_start))"
echo "blobs_kb_before=$before blobs_kb_after=$after"
[ "$before" = "$after" ] && echo "RECREATE_REUSED_THE_WEIGHTS" || echo "RECREATE_CHANGED_THE_WEIGHTS"
# Three independent signals, because "it was fast" alone would also be true of a
# cached HTTP 304: the weights are byte-identical in size, the entrypoint never
# reached its pull branch, and the recreate took seconds rather than minutes.
if docker logs tick059-ollama 2>&1 | grep -q 'not in the volume yet, pulling it once'; then
    echo "RE_DOWNLOADED (unexpected)"
else
    echo "NO_PULL_ON_RECREATE"
fi

# --- 5. Discrimination: does the pin actually bite? ---------------------------
step "5a. A wrong digest must refuse to serve"
start_model_server "$MODEL" "sha256:$(printf '0%.0s' $(seq 64))" "$WARM_VOLUME"
sleep 12
echo "exit_code=$(docker inspect -f '{{.State.ExitCode}}' tick059-ollama)"
echo "running=$(docker inspect -f '{{.State.Running}}' tick059-ollama)"
docker logs tick059-ollama 2>&1 | grep -E 'MISMATCH|expected|actual' | head -5

step "5b. The healthcheck must fail on a volume with no model"
docker volume rm "$COLD_VOLUME" >/dev/null 2>&1 || true
start_model_server "$MODEL" "$DIGEST" "$COLD_VOLUME"
sleep 10
if docker exec tick059-ollama /usr/local/bin/aeai-ollama-healthcheck 2>&1; then
    echo "HEALTHCHECK_PASSED (unexpected -- it should fail while the model is absent)"
else
    echo "HEALTHCHECK_CORRECTLY_FAILED"
fi
docker rm -f tick059-ollama >/dev/null

step "5c. A healthy warm server must pass the same healthcheck"
start_model_server "$MODEL" "$DIGEST" "$WARM_VOLUME"
wait_healthy 300 >/dev/null
docker exec tick059-ollama /usr/local/bin/aeai-ollama-healthcheck \
    && echo "HEALTHCHECK_CORRECTLY_PASSED"

step "6. Host GPU visibility (AC5): what the model server actually had"
docker exec tick059-ollama sh -c 'ls /dev/nvidia* /dev/dri 2>&1 | head -3' || true
docker logs tick059-ollama 2>&1 | grep -iE 'no compatible gpus|inference compute|library=' | head -3 || true

echo
echo "LIVE_VERIFICATION_COMPLETE"
