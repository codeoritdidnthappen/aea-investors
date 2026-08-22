#!/usr/bin/env bash
# TICK-054 live verification driver.
#
# Wraps verify_deferred_chat_launch.mjs with the three things that make its result
# trustworthy and repeatable:
#
#   1. Re-renders rendered_panel.html from THIS worktree's PortalChatController using
#      the OpenEMR container's own PHP, so the harness can never verify stale markup.
#      (The container bind-mounts the module from the main checkout, so the running
#      stack still serves the pre-TICK-054 panel -- see the harness header.)
#   2. Resets the seeded synthetic patient's portal password to a known value
#      immediately before the run. Recorded as a deviation in
#      LIVE_VERIFICATION_2026-08-22.md; same category as the reset already on the
#      record in evidence/TICK-045/FIX_VERIFICATION.md.
#   3. Diffs the ai-server's own request log across the run, so "how many
#      authorizations were started" has a second, server-side witness that does not
#      depend on the browser-side capture being complete.
#
# Run from the worktree root:  bash evidence/TICK-054/run_live_verification.sh
set -euo pipefail

PORTAL_PASS="${TICK054_PORTAL_PASS:-Tick054Verify!2026}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1. render this worktree's panel with the container's PHP =="
docker cp "$ROOT/openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php" \
  local-openemr-1:/tmp/PortalChatController.php >/dev/null
docker cp "$HERE/render_panel_probe.php" local-openemr-1:/tmp/probe.php >/dev/null
docker exec local-openemr-1 php -l /tmp/PortalChatController.php
docker exec local-openemr-1 php /tmp/probe.php | sed -n '/<!--PANEL-->/,$p' | tail -n +2 \
  > "$HERE/rendered_panel.html"
grep -q 'data-src="' "$HERE/rendered_panel.html"
echo "   rendered_panel.html: $(wc -c < "$HERE/rendered_panel.html") bytes"

echo "== 2. reset the seeded patient's portal password =="
# The SQL goes in over stdin: the bcrypt hash is full of '$', which an inner
# `sh -c "..."` would happily expand into nothing.
docker exec local-openemr-1 php -r "echo password_hash('${PORTAL_PASS}', PASSWORD_DEFAULT);" \
  > "$TMP/hash.txt"
printf "UPDATE patient_access_onsite SET portal_pwd='%s', portal_pwd_status=1 WHERE pid=1;\n" \
  "$(cat "$TMP/hash.txt")" > "$TMP/reset.sql"
docker exec -i local-mariadb-1 sh -c 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" openemr' \
  < "$TMP/reset.sql"
echo "   pid 1 portal password reset"

launch_hits() {
  docker logs local-ai-server-1 2>&1 | grep -c 'GET /oauth/launch' || true
}
BEFORE="$(launch_hits)"
echo "== 3. run the capture (ai-server /oauth/launch hits before: ${BEFORE}) =="
set +e
TICK054_PORTAL_PASS="$PORTAL_PASS" node "$HERE/verify_deferred_chat_launch.mjs"
STATUS=$?
set -e
AFTER="$(launch_hits)"
echo
echo "ai-server /oauth/launch hits: before=${BEFORE} after=${AFTER} delta=$((AFTER - BEFORE))"
echo "(expected delta: 2 -- one for the mouse open, one for the keyboard open. The"
echo " collapse/re-open case stubs the launch response at the browser, so its single"
echo " request never reaches the ai-server.)"
exit "$STATUS"
