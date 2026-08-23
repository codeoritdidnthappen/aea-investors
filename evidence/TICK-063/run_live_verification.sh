#!/usr/bin/env sh
# TICK-063: drive real chat turns through the real local model.
#
# What this proves: that `llama3.1:8b-instruct-q4_K_M` (D17), reading the shipped
# `acceptance-tool-call-v1` prompt, actually routes real conversations -- including the
# multi-turn ones the corpus cannot cover, because a corpus case is one turn -- and that
# the tool call it emits reaches the right service with the validator's values.
#
# What it does NOT prove: the OpenEMR wire format, which is mocked here and already
# proven live in evidence/TICK-049 and evidence/TICK-031; and the containerised
# deployment, which needs `deploy/local` rebuilt with LLM_PROVIDER=ollama. See
# VERIFICATION_2026-08-23.md, "Not verified here".
#
# Like evidence/TICK-062's script this uses a model server deliberately separate from
# `deploy/local` -- its own name, its own port, its own volume -- so a verification run
# never disturbs a stack someone is using, and never inherits its state either.
set -eu

CONTAINER=${CONTAINER:-tick063-ollama}
PORT=${PORT:-11499}
IMAGE=ollama/ollama:0.32.15
MODEL=${MODEL:-llama3.1:8b-instruct-q4_K_M}
OUT=evidence/TICK-063

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER" -p "${PORT}:11434" \
        -v "${CONTAINER}-models:/root/.ollama" "$IMAGE"
    # `ollama list` round-trips the HTTP API, so it succeeds only once genuinely serving.
    until docker exec "$CONTAINER" ollama list >/dev/null 2>&1; do sleep 2; done
    docker exec "$CONTAINER" ollama pull "$MODEL"
fi

# The ID column's first 12 characters are the sha256 of the registry manifest, which is
# what deploy/local/ollama-entrypoint.sh pins. Confirms the weights under test are the
# weights AI_USAGE.md names.
docker exec "$CONTAINER" ollama list

MODEL_BASE_URL="http://localhost:${PORT}" MODEL="$MODEL" PYTHONPATH=. \
    uv run --locked python evidence/TICK-063/live_turns.py 2>/dev/null \
    | tee "${OUT}/run-live-turns.txt"
