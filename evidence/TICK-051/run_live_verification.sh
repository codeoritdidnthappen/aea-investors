#!/usr/bin/env bash
# TICK-051 live verification driver.
#
# Wraps verify_signin_lands_on_dashboard.mjs with the things that make its result
# trustworthy and repeatable, following the pattern TICK-054/TICK-055 set:
#
#   1. Rebuilds `ai-server` from THIS worktree. The ai-server is NOT bind-mounted
#      (deploy/local/docker-compose.yml builds it from context), so without
#      `--build` the run would verify the previously-built image and silently
#      measure the old, chat-landing server. Recorded hazard for this repo.
#   2. Proves host and container agree before measuring anything: the ai-server
#      sources are diffed by checksum, the portal module PHP too, and the container
#      is asked for the two split settings by name.
#   3. Asserts the old single setting is gone from the container's environment --
#      the rename is load-bearing (AC10) and a stale value would mean a stale image.
#   4. Runs the Origin discipline straight at the server, before the browser, so
#      "the CSRF check survived the split" has a witness that does not depend on
#      the browser capture being complete (AC9).
#   5. Resets the seeded synthetic patient's portal password to a known value
#      immediately before the run -- same deviation, same category, as
#      evidence/TICK-054 and evidence/TICK-055.
#
# Run from the worktree root:  bash evidence/TICK-051/run_live_verification.sh
set -euo pipefail

PORTAL_PASS="${TICK051_PORTAL_PASS:-Tick051Verify!2026}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1. rebuild ai-server from this worktree (it is not bind-mounted) =="
docker compose --project-directory "$ROOT/deploy/local" \
  -f "$ROOT/deploy/local/docker-compose.yml" up -d --build ai-server openemr 2>&1 | tail -5
sleep 8

echo "== 2. host and container must agree before anything is measured =="
for f in ai_server/app/main.py ai_server/app/auth.py; do
  HOST_SUM="$(md5 -q "$ROOT/$f")"
  CONT_SUM="$(docker exec local-ai-server-1 md5sum "/app/$f" | cut -d' ' -f1)"
  [ "$HOST_SUM" = "$CONT_SUM" ] || { echo "   MISMATCH $f host=$HOST_SUM container=$CONT_SUM"; exit 1; }
  echo "   $f matches: $HOST_SUM"
done
HOST_SUM="$(md5 -q "$ROOT/openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php")"
CONT_SUM="$(docker exec local-openemr-1 md5sum \
  /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/aeai-portal-chat/src/Controller/PortalChatController.php \
  | cut -d' ' -f1)"
[ "$HOST_SUM" = "$CONT_SUM" ] || { echo "   MODULE MISMATCH host=$HOST_SUM container=$CONT_SUM"; exit 1; }
echo "   portal module PHP matches: $HOST_SUM"

echo "== 3. the split settings are what the container actually holds (AC1/AC8/AC10) =="
echo -n "   AI_SESSION_DASHBOARD_REDIRECT_URI="
docker exec local-ai-server-1 printenv AI_SESSION_DASHBOARD_REDIRECT_URI
echo -n "   AI_SESSION_CHAT_ORIGIN="
docker exec local-ai-server-1 printenv AI_SESSION_CHAT_ORIGIN
if docker exec local-ai-server-1 printenv AI_SESSION_SUCCESS_REDIRECT_URI >/dev/null 2>&1; then
  echo "   the renamed-away AI_SESSION_SUCCESS_REDIRECT_URI is still set -- stale image or stale .env"
  exit 1
fi
echo "   AI_SESSION_SUCCESS_REDIRECT_URI is absent (renamed, not reused)"

echo "== 4. the Origin discipline, straight at the running server (AC9) =="
turn() { curl -sk -o /dev/null -w '%{http_code}' -X POST https://chat.localhost/api/chat \
  -H 'Content-Type: application/json' -d '{"message":"hi"}' "$@"; }
echo "   chat origin (own fetch) -> $(turn -H 'Origin: https://chat.localhost')  (want 401: origin ok, no session)"
echo "   dashboard origin        -> $(turn -H 'Origin: https://emr.localhost')  (want 403)"
echo "   foreign origin          -> $(turn -H 'Origin: https://attacker.test')  (want 403)"
echo "   no Origin at all        -> $(turn)  (want 403)"
loc() { curl -sk -o /dev/null -w '%{http_code} %{redirect_url}' "$@"; }
echo "   callback, denial        -> $(loc -H 'Sec-Fetch-Dest: document' \
  'https://chat.localhost/oauth/callback?error=access_denied&error_description=x&state=y')  (want 303 -> dashboard)"

echo "== 5. reset the seeded patient's portal password =="
# The SQL goes in over stdin: the bcrypt hash is full of '$', which an inner
# `sh -c "..."` would happily expand into nothing.
docker exec local-openemr-1 php -r "echo password_hash('${PORTAL_PASS}', PASSWORD_DEFAULT);" \
  > "$TMP/hash.txt"
printf "UPDATE patient_access_onsite SET portal_pwd='%s', portal_pwd_status=1 WHERE pid=1;\n" \
  "$(cat "$TMP/hash.txt")" > "$TMP/reset.sql"
docker exec -i local-mariadb-1 sh -c 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" openemr' \
  < "$TMP/reset.sql"
echo "   pid 1 portal password reset"

echo "== 6. run the capture =="
TICK051_PORTAL_PASS="$PORTAL_PASS" node "$HERE/verify_signin_lands_on_dashboard.mjs"
