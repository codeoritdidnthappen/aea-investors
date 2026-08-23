#!/usr/bin/env sh
# TICK-062: reproduce the acceptance-corpus run that selected the local model.
#
# What this proves: that the harness runs the corpus against a real model server, scores
# the two bars separately, and reports every wrong write individually.
#
# What it does NOT prove: anything about vLLM. vLLM does not run on this development
# host (Apple Silicon, arm64, no CUDA) and no vLLM adapter exists in the repo yet --
# TICK-066 owns that and depends on this ticket. The `--compare` path is exercised here
# against two Ollama quantisations instead, which is the same divergence mechanism
# (different numerics behind one model name) on the runtime that is actually available.
# See VERIFICATION_2026-08-23.md, "Not verified here".
#
# This runs a model server in a container that is deliberately separate from
# `deploy/local` -- its own name, its own port, its own volume -- so an eval run never
# disturbs a stack someone is using, and never inherits its state either.
set -eu

CONTAINER=tick062-ollama
PORT=11499
BASE_URL="http://localhost:${PORT}"
IMAGE=ollama/ollama:0.32.15
PRIMARY=qwen2.5:7b-instruct-q4_K_M
CHALLENGER=llama3.1:8b-instruct-q4_K_M
OUT=evidence/TICK-062

step() {
    printf '\n=== %s ===\n' "$1"
}

step "model server"
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER" -p "${PORT}:11434" \
        -v "${CONTAINER}-models:/root/.ollama" "$IMAGE"
    # `ollama list` round-trips the HTTP API, so it succeeds only once genuinely serving.
    until docker exec "$CONTAINER" ollama list >/dev/null 2>&1; do sleep 2; done
fi
docker exec "$CONTAINER" ollama pull "$PRIMARY"
docker exec "$CONTAINER" ollama pull "$CHALLENGER"

step "pinned digest"
# The ID column's first 12 characters are the sha256 of the registry manifest, which is
# what deploy/local/ollama-entrypoint.sh pins. Confirms the weights under test are the
# weights AI_USAGE.md names.
docker exec "$CONTAINER" ollama list

step "the full corpus, primary model"
uv run --locked python -m scripts.evaluate_acceptance_corpus \
    --backend ollama --base-url "$BASE_URL" --model "$PRIMARY" \
    --record "eval/replays/ollama-qwen2.5-7b-instruct-q4_K_M.json" \
    | tee "${OUT}/run-qwen2.5-7b-instruct-q4_K_M.txt" || true

step "the full corpus, challenger model"
uv run --locked python -m scripts.evaluate_acceptance_corpus \
    --backend ollama --base-url "$BASE_URL" --model "$CHALLENGER" \
    --record "/tmp/tick062-llama31-8b.json" \
    | tee "${OUT}/run-llama3.1-8b-instruct-q4_K_M.txt" || true

step "the full tool-call schema is not compilable on this runtime"
# Expected to fail with 400 "failed to parse grammar". Recorded because TICK-060 offers
# tool_call_json_schema() as the thing to constrain generation with, and on Ollama it is
# not available -- so parse_tool_call() is carrying the whole guarantee here.
uv run --locked python -m scripts.evaluate_acceptance_corpus \
    --backend ollama --base-url "$BASE_URL" --model "$PRIMARY" \
    --response-format tool_call --limit 1 || true

step "the CI subset, replayed, contacting no server"
uv run --locked python -m scripts.evaluate_acceptance_corpus --ci-subset \
    --replay "eval/replays/ollama-qwen2.5-7b-instruct-q4_K_M.json" \
    | tee "${OUT}/run-ci-subset-replay.txt"

step "done"
echo "Leave the container running to re-run, or remove it with:"
echo "  docker rm -f ${CONTAINER} && docker volume rm ${CONTAINER}-models"
