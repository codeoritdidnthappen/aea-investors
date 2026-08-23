#!/usr/bin/env sh
# TICK-067: record what the pinned local model says on turns no capability covers.
#
# The answer is specific to the model, the quantisation AND the backend, so all three
# are pinned here and printed below before anything runs. Re-running this against
# different weights produces a different finding, which is the point.
#
# Three conditions, in increasing order of how much the prompt constrains the model:
#
#   bare         no system prompt at all -- the instruct tune answering as itself
#   baseline     the production prompt TICK-062 measured, unmodified
#   constrained  that prompt plus the strongest instruction-only constraints worth
#                trying, to answer whether prompting alone is sufficient (AC5)
#
# Like TICK-062's harness, this runs a model server deliberately separate from
# `deploy/local` -- its own name, its own port, its own volume -- so a probe run never
# disturbs a stack someone is using and never inherits its state.
set -eu

CONTAINER=tick067-ollama
PORT=11499
BASE_URL="http://localhost:${PORT}"
IMAGE=ollama/ollama:0.32.15
MODEL=llama3.1:8b-instruct-q4_K_M
OUT=evidence/TICK-067

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
docker exec "$CONTAINER" ollama pull "$MODEL"

step "pinned digest"
# The ID column's first 12 characters are the sha256 of the registry manifest, which is
# what deploy/local/ollama-entrypoint.sh pins. It must read 46e0c10c039e -- the digest
# in deploy/local/docker-compose.yml. Different weights, different finding.
docker exec "$CONTAINER" ollama --version
docker exec "$CONTAINER" ollama list

for VARIANT in bare baseline constrained; do
    TRANSCRIPT="${OUT}/transcript-${VARIANT}-llama3.1-8b-instruct-q4_K_M.json"

    step "the uncovered-turn corpus, ${VARIANT} -- live"
    uv run --locked python -m scripts.probe_uncovered_turns \
        --backend ollama --base-url "$BASE_URL" --model "$MODEL" \
        --variant "$VARIANT" --record "$TRANSCRIPT" >/dev/null

    step "the uncovered-turn corpus, ${VARIANT} -- rendered from what was recorded"
    # The committed run-*.txt is rendered from the transcript rather than teed from the
    # live call, so the report and the bytes it describes can never drift apart, and so
    # anyone can regenerate the report from the committed evidence with no model server.
    uv run --locked python -m scripts.probe_uncovered_turns \
        --variant "$VARIANT" --replay "$TRANSCRIPT" \
        | tee "${OUT}/run-${VARIANT}.txt"
done

step "the recorded transcripts replay with no server"
# What CI re-derives on every run: the finding's claims come back out of the recorded
# bytes rather than being trusted. See ai_server/tests/test_probe_uncovered_turns.py.
uv run --locked --group dev pytest ai_server/tests/test_probe_uncovered_turns.py -q

step "done"
echo "Leave the container running to re-run, or remove it with:"
echo "  docker rm -f ${CONTAINER} && docker volume rm ${CONTAINER}-models"
