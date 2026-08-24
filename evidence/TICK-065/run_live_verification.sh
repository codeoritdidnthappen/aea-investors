#!/usr/bin/env sh
# TICK-065: verify the chat's behaviour with the model server stopped.
#
# What this proves, against a real server over real HTTP rather than a test client:
#
#   1. `/health` reports `model_server: unavailable` while the server is stopped, and
#      flips to `ok` when one is started -- so the probe observes something, rather than
#      being unconditionally pessimistic.
#   2. A patient's turn is answered with the honest unavailable message.
#   3. A turn that would *write* is answered the same way, and the patient's address in
#      OpenEMR's own database is byte-identical before and after. Failing closed on
#      writes is the point, so the write claim is checked at the record, not at the reply.
#   4. The OpenEMR patient portal keeps serving throughout.
#
# The AI server runs from this worktree on a spare port with its own throwaway SQLite
# session database, and is stopped again at the end -- so a stack someone is using is
# never disturbed, and this run never inherits its state either (the same discipline
# evidence/TICK-063's script follows for its model server).
#
# The model server is "stopped" by pointing OLLAMA_HOST at a port nothing is listening
# on, which is what a stopped container looks like to the AI server: a refused
# connection. Step 1 then starts a real Ollama to show the probe recovering.
#
#   sh evidence/TICK-065/run_live_verification.sh
set -eu

PORT=${PORT:-8765}
DEAD_MODEL_PORT=${DEAD_MODEL_PORT:-11498}
LIVE_MODEL_CONTAINER=${LIVE_MODEL_CONTAINER:-tick065-ollama}
LIVE_MODEL_PORT=${LIVE_MODEL_PORT:-11497}
OUT=evidence/TICK-065
WORK=$(mktemp -d)
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

export AI_SESSION_DATABASE_PATH="$WORK/sessions.sqlite3"
export AI_SESSION_ENCRYPTION_KEY="a2V5LWZvci1saXZlLXZlcmlmaWNhdGlvbi0zMmJ5dGU="
export OPENEMR_OAUTH_AUTHORIZE_URL="https://openemr/oauth2/default/authorize"
export OPENEMR_OAUTH_TOKEN_URL="https://openemr/oauth2/default/token"
export OPENEMR_OAUTH_JWKS_URL="https://openemr/oauth2/default/jwks"
export OPENEMR_OAUTH_ISSUER="https://openemr"
export OPENEMR_OAUTH_CLIENT_ID="synthetic-client"
export OPENEMR_OAUTH_CLIENT_SECRET="synthetic-secret"
export OPENEMR_OAUTH_REDIRECT_URI="http://127.0.0.1:${PORT}/oauth/callback"
export AI_SESSION_DASHBOARD_REDIRECT_URI="https://emr.localhost/portal/home.php"
export AI_SESSION_CHAT_ORIGIN="http://127.0.0.1:${PORT}"
export OPENEMR_PORTAL_API_BASE_URL="https://openemr/apis/default"
# The front door is local, and it is pointed at nothing.
export LLM_PROVIDER=ollama
export LLM_MODEL=llama3.1:8b-instruct-q4_K_M
export OLLAMA_HOST="http://127.0.0.1:${DEAD_MODEL_PORT}"

echo "### 0. The model server is not listening on ${DEAD_MODEL_PORT}"
if nc -z 127.0.0.1 "$DEAD_MODEL_PORT" 2>/dev/null; then
    echo "ABORT: something is listening on ${DEAD_MODEL_PORT}; set DEAD_MODEL_PORT" >&2
    exit 1
fi
echo "confirmed: connection to 127.0.0.1:${DEAD_MODEL_PORT} is refused"

echo
echo "### 1. The AI server, built from this worktree, on port ${PORT}"
git -C . rev-parse --short HEAD
uv run --locked uvicorn ai_server.app.main:app --host 127.0.0.1 --port "$PORT" \
    >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
until curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; do
    kill -0 "$SERVER_PID" 2>/dev/null || { cat "$WORK/server.log"; exit 1; }
    sleep 1
done

echo
echo "### 2. /health with the model server stopped"
curl -s "http://127.0.0.1:${PORT}/health" | python3 -m json.tool

echo
echo "### 3. The patient's address in OpenEMR, before the turn"
BEFORE=$(docker exec local-mariadb-1 sh -c \
    'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B openemr -e \
     "select concat(pid,\"|\",street,\"|\",city,\"|\",state,\"|\",postal_code) \
      from patient_data order by pid"')
echo "$BEFORE"

echo
echo "### 4. Two turns from a signed-in patient: a plain one, then one that would write"
COOKIE=$(PYTHONPATH=. uv run --locked python "$OUT/mint_session.py")
for MESSAGE in "Hi, can you help me?" "Change my address to 88 Larch Street, Toms River NJ 08753"; do
    echo "--- patient: ${MESSAGE}"
    echo "--- assistant:"
    curl -s -X POST "http://127.0.0.1:${PORT}/api/chat" \
        -H "Origin: http://127.0.0.1:${PORT}" \
        -H "Content-Type: application/json" \
        -b "ai_session=${COOKIE}" \
        --data "$(python3 -c 'import json,sys; print(json.dumps({"message": sys.argv[1]}))' "$MESSAGE")"
    echo
done

echo
echo "### 5. The patient's address in OpenEMR, after the turn"
AFTER=$(docker exec local-mariadb-1 sh -c \
    'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B openemr -e \
     "select concat(pid,\"|\",street,\"|\",city,\"|\",state,\"|\",postal_code) \
      from patient_data order by pid"')
echo "$AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
    echo "RESULT: unchanged -- no write was attempted while the model was unavailable"
else
    echo "RESULT: CHANGED -- a write executed during an outage. This is a failure." >&2
    exit 1
fi

echo
echo "### 6. The OpenEMR patient portal, unaffected"
curl -sk -o /dev/null -w 'portal login page: HTTP %{http_code}\n' \
    https://emr.localhost/portal/index.php
curl -sk -o /dev/null -w 'portal home:       HTTP %{http_code}\n' \
    https://emr.localhost/portal/home.php

echo
echo "### 7. The probe recovers: start a real model server and re-read /health"
docker rm -f "$LIVE_MODEL_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$LIVE_MODEL_CONTAINER" -p "${LIVE_MODEL_PORT}:11434" \
    ollama/ollama:0.32.15 >/dev/null
until docker exec "$LIVE_MODEL_CONTAINER" ollama list >/dev/null 2>&1; do sleep 2; done
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
OLLAMA_HOST="http://127.0.0.1:${LIVE_MODEL_PORT}" \
    uv run --locked uvicorn ai_server.app.main:app --host 127.0.0.1 --port "$PORT" \
    >"$WORK/server2.log" 2>&1 &
SERVER_PID=$!
until curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; do sleep 1; done
curl -s "http://127.0.0.1:${PORT}/health" | python3 -m json.tool
docker rm -f "$LIVE_MODEL_CONTAINER" >/dev/null 2>&1 || true

echo
echo "### done"
