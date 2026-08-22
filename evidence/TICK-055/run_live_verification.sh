#!/usr/bin/env bash
# TICK-055 live verification driver.
#
# Wraps verify_portal_signout_ends_chat.mjs with the things that make its result
# trustworthy and repeatable:
#
#   1. Rebuilds `ai-server` from THIS worktree. The ai-server is NOT bind-mounted
#      (deploy/local/docker-compose.yml builds it from context), so without
#      `--build` the run would verify the previously-built image and silently
#      measure the old, logout-less server. Recorded hazard for this repo.
#   2. Proves host and container agree before measuring anything: the module PHP
#      is diffed by checksum, and the ai-server is asked whether it actually has
#      the logout route and the configured portal origin.
#   3. Resets the seeded synthetic patient's portal password to a known value
#      immediately before the run -- same deviation, same category, as
#      evidence/TICK-054/run_live_verification.sh and evidence/TICK-045.
#   4. Diffs the ai-server's own request log across the run, so "the sign-out
#      beacon arrived and was accepted" has a second, server-side witness that
#      does not depend on the browser-side capture being complete.
#
# Run from the worktree root:  bash evidence/TICK-055/run_live_verification.sh
set -euo pipefail

PORTAL_PASS="${TICK055_PORTAL_PASS:-Tick055Verify!2026}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1. rebuild ai-server from this worktree (it is not bind-mounted) =="
docker compose --project-directory "$ROOT/deploy/local" \
  -f "$ROOT/deploy/local/docker-compose.yml" up -d --build ai-server openemr 2>&1 | tail -5
sleep 5

echo "== 2. host and container must agree before anything is measured =="
HOST_SUM="$(md5 -q "$ROOT/openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php")"
CONT_SUM="$(docker exec local-openemr-1 md5sum \
  /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/aeai-portal-chat/src/Controller/PortalChatController.php \
  | cut -d' ' -f1)"
[ "$HOST_SUM" = "$CONT_SUM" ] || { echo "   MODULE MISMATCH host=$HOST_SUM container=$CONT_SUM"; exit 1; }
echo "   module PHP matches: $HOST_SUM"
docker exec local-ai-server-1 grep -q 'api/logout' /app/ai_server/app/main.py \
  || { echo "   ai-server image has no /api/logout -- stale build"; exit 1; }
echo "   ai-server has /api/logout"
echo -n "   ai-server AI_SESSION_PORTAL_ORIGIN="
docker exec local-ai-server-1 printenv AI_SESSION_PORTAL_ORIGIN
echo -n "   openemr  AEAI_PORTAL_CHAT_LOGOUT_URL="
docker exec local-openemr-1 printenv AEAI_PORTAL_CHAT_LOGOUT_URL

echo "== 3. the Origin discipline, straight at the running server =="
code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
echo "   off-origin POST      -> $(code -X POST https://chat.localhost/api/logout -H 'Origin: https://evil.test')  (want 403)"
echo "   no-Origin POST       -> $(code -X POST https://chat.localhost/api/logout)  (want 403)"
echo "   portal-origin POST   -> $(code -X POST https://chat.localhost/api/logout -H 'Origin: https://emr.localhost')  (want 204)"
echo "   chat-origin POST     -> $(code -X POST https://chat.localhost/api/logout -H 'Origin: https://chat.localhost')  (want 204)"
echo "   GET (must not exist) -> $(code https://chat.localhost/api/logout)  (want 405)"
echo "   /api/chat w/ portal  -> $(code -X POST https://chat.localhost/api/chat -H 'Origin: https://emr.localhost' -H 'Content-Type: application/json' -d '{"message":"hi"}')  (want 403)"

echo "== 4. reset the seeded patient's portal password =="
# The SQL goes in over stdin: the bcrypt hash is full of '$', which an inner
# `sh -c "..."` would happily expand into nothing.
docker exec local-openemr-1 php -r "echo password_hash('${PORTAL_PASS}', PASSWORD_DEFAULT);" \
  > "$TMP/hash.txt"
printf "UPDATE patient_access_onsite SET portal_pwd='%s', portal_pwd_status=1 WHERE pid=1;\n" \
  "$(cat "$TMP/hash.txt")" > "$TMP/reset.sql"
docker exec -i local-mariadb-1 sh -c 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" openemr' \
  < "$TMP/reset.sql"
echo "   pid 1 portal password reset"

logout_hits() { docker logs local-ai-server-1 2>&1 | grep -c 'POST /api/logout' || true; }
logout_204s() { docker logs local-ai-server-1 2>&1 | grep 'POST /api/logout' | grep -c '204' || true; }
BEFORE_HITS="$(logout_hits)"; BEFORE_204="$(logout_204s)"
echo "== 5. run the capture (ai-server logout hits before: ${BEFORE_HITS}, of which 204: ${BEFORE_204}) =="
set +e
TICK055_PORTAL_PASS="$PORTAL_PASS" node "$HERE/verify_portal_signout_ends_chat.mjs"
STATUS=$?
set -e
AFTER_HITS="$(logout_hits)"; AFTER_204="$(logout_204s)"
echo
echo "ai-server POST /api/logout: before=${BEFORE_HITS} after=${AFTER_HITS} delta=$((AFTER_HITS - BEFORE_HITS))"
echo "                  of which 204: before=${BEFORE_204} after=${AFTER_204} delta=$((AFTER_204 - BEFORE_204))"
echo "(expected: exactly one more request, accepted with 204 -- the sign-out beacon.)"
exit "$STATUS"
