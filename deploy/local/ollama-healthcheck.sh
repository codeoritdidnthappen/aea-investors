#!/usr/bin/env sh
# TICK-059: report healthy only when the *pinned* model can actually answer.
#
# TICK-057's lesson, applied to the model server: readiness is not enough. A
# container that is up and listening but holds no model still fails every chat
# turn, and "the process is running" is precisely the signal that let a broken
# portal look healthy for an entire session. So this asserts three things, and
# `docker ps` shows unhealthy unless all three hold:
#
#   1. The server is genuinely serving -- `ollama list` round-trips the same HTTP
#      API the AI server calls, so it fails while the process is still starting.
#   2. The pinned model is present in the volume.
#   3. Its manifest still hashes to the pinned digest.
#
# (3) repeats the entrypoint's check rather than trusting that it ran, because the
# two answer different questions at different times: the entrypoint decides whether
# to serve at all, this decides whether the thing serving right now is still the
# reviewed model. The path derivation below is duplicated from the entrypoint on
# purpose -- each script has to stand alone as a container command, and a shared
# file sourced by both would be a third thing to keep in step for eight lines of
# dash.
#
# During a cold start this reports unhealthy for as long as the pull takes, which
# is correct and is the point: the model server is not ready to answer yet.
set -eu

: "${LLM_MODEL:?LLM_MODEL is required}"
: "${LLM_MODEL_DIGEST:?LLM_MODEL_DIGEST is required}"

MODELS_DIR=/root/.ollama/models

name=${LLM_MODEL%:*}
tag=${LLM_MODEL##*:}
case "$name" in
    */*) : ;;
    *) name="library/$name" ;;
esac
MANIFEST="$MODELS_DIR/manifests/registry.ollama.ai/$name/$tag"
EXPECTED=${LLM_MODEL_DIGEST#sha256:}

if ! ollama list >/dev/null 2>&1; then
    echo "model server is not serving yet" >&2
    exit 1
fi

if [ ! -f "$MANIFEST" ]; then
    echo "pinned model $LLM_MODEL is not in the volume (expected $MANIFEST)" >&2
    exit 1
fi

ACTUAL=$(sha256sum "$MANIFEST" | cut -d' ' -f1)
if [ "$ACTUAL" != "$EXPECTED" ]; then
    echo "pinned model digest mismatch: expected sha256:$EXPECTED, got sha256:$ACTUAL" >&2
    exit 1
fi
