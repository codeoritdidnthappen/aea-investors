#!/usr/bin/env sh
# TICK-059: serve the pinned local model, and refuse to serve anything else.
#
# LOCAL_LLM_SPEC D7 puts Ollama under the development half of the topology, and D6
# puts the model's output on a path that reaches a medical record. NFR-15 already
# pins the OpenEMR and MariaDB images for that reason; a model tag is the same
# problem wearing different clothes. `qwen2.5:7b-instruct-q4_K_M` is a tag, and a
# tag is a mutable pointer: upstream can repoint it at a requantised or retrained
# build and the next `ollama pull` on a fresh volume would serve different weights,
# under the same name, with nothing reporting a change.
#
# So the model is pinned twice -- by name (LLM_MODEL) and by digest
# (LLM_MODEL_DIGEST) -- and this script is what makes the second pin bite:
#
#   1. If the pinned model is not in the volume, pull it. That is the "no manual
#      pull step" half of the ticket: a clean checkout gets a working model server
#      from `docker compose up` alone, with no README step to forget.
#   2. Verify the digest, always -- on a fresh pull *and* on every later start.
#      Ollama stores the registry manifest verbatim, so the sha256 of that file is
#      the model's identity: it is exactly what the registry served, exactly what
#      `/api/tags` reports as `digest`, and the `ollama list` ID column is its
#      first 12 characters. Confirmed all three ways, see evidence/TICK-059.
#   3. On a mismatch, exit non-zero *without* serving. A model server that answers
#      with the wrong weights is worse than one that is down: D12 already makes an
#      unavailable model an honest, visible outage, whereas wrong-but-answering is
#      the silent-degradation failure this repo has now had twice.
#
# Only `sha256sum`, `grep` and the `ollama` CLI are used, all of which the stock
# image has -- there is no curl, wget, jq or python3 in it (checked), and adding a
# package manager step to get one would put a network fetch in the image build.
set -eu

: "${LLM_MODEL:?LLM_MODEL is required: the model this server pins and serves}"
: "${LLM_MODEL_DIGEST:?LLM_MODEL_DIGEST is required: the sha256 LLM_MODEL must resolve to}"

MODELS_DIR=/root/.ollama/models

# Map `namespace/name:tag` (or the `name:tag` shorthand, which Ollama reads as
# `library/name:tag`) onto the manifest path Ollama writes it to.
manifest_path() {
    reference=$1
    name=${reference%:*}
    tag=${reference##*:}
    case "$name" in
        */*) : ;;
        *) name="library/$name" ;;
    esac
    printf '%s/manifests/registry.ollama.ai/%s/%s' "$MODELS_DIR" "$name" "$tag"
}

MANIFEST=$(manifest_path "$LLM_MODEL")
# Pinned as `sha256:<hex>` to match how every other digest in this stack is written,
# but compared as bare hex because that is what `sha256sum` emits.
EXPECTED=${LLM_MODEL_DIGEST#sha256:}

echo "aeai-ollama: pinned model $LLM_MODEL @ sha256:$EXPECTED"

# The server has to be up for `ollama pull` to talk to, so start it first and hand
# it the terminal signals at the end via `wait`.
ollama serve &
server_pid=$!

# `ollama list` is the readiness probe: it round-trips through the same HTTP API
# the AI server will use, so it succeeds only once the server is genuinely serving.
waited=0
until ollama list >/dev/null 2>&1; do
    if ! kill -0 "$server_pid" 2>/dev/null; then
        echo "aeai-ollama: the model server exited during startup" >&2
        exit 1
    fi
    if [ "$waited" -ge 60 ]; then
        echo "aeai-ollama: the model server did not become ready within 60s" >&2
        exit 1
    fi
    sleep 1
    waited=$((waited + 1))
done

if [ ! -f "$MANIFEST" ]; then
    echo "aeai-ollama: $LLM_MODEL is not in the volume yet, pulling it once"
    echo "aeai-ollama: a cold volume downloads the whole model, which is the slow first start"
    ollama pull "$LLM_MODEL"
fi

if [ ! -f "$MANIFEST" ]; then
    echo "aeai-ollama: $LLM_MODEL is still absent after a pull (expected $MANIFEST)" >&2
    exit 1
fi

ACTUAL=$(sha256sum "$MANIFEST" | cut -d' ' -f1)
if [ "$ACTUAL" != "$EXPECTED" ]; then
    # Either upstream repointed the tag, or the volume holds a model pulled before
    # the pin was set. Both mean the bytes about to answer a question about a
    # patient are not the bytes that were reviewed.
    echo "aeai-ollama: PINNED MODEL DIGEST MISMATCH -- refusing to serve" >&2
    echo "aeai-ollama:   model    $LLM_MODEL" >&2
    echo "aeai-ollama:   expected sha256:$EXPECTED" >&2
    echo "aeai-ollama:   actual   sha256:$ACTUAL" >&2
    echo "aeai-ollama:   the tag has moved, or the volume predates the pin." >&2
    echo "aeai-ollama:   Re-pull it (\`docker compose down -v\` drops the model volume)," >&2
    echo "aeai-ollama:   or update LLM_MODEL_DIGEST if the new build was reviewed." >&2
    exit 1
fi

echo "aeai-ollama: $LLM_MODEL verified against its pinned digest, serving"
wait "$server_pid"
