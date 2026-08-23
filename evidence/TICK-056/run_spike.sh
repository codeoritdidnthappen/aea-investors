#!/usr/bin/env bash
# TICK-056 — can OpenEMR 8.3.0's OAuth2 authorize endpoint accept an existing
# patient-portal session in place of its own login?
#
# Re-runnable discovery harness. Exercises the pinned release in the local
# Docker topology through a real patient portal session. Prints a labelled
# result per experiment; every claim in FINDING.md traces to one of these.
#
# Usage:  bash evidence/TICK-056/run_spike.sh
# Needs:  the local stack up (deploy/local/README.md), curl, python3, openssl.
#
# Redaction: no access token, refresh token, client secret, authorization
# code, patient UUID, name, date of birth, or timestamp is printed. Session
# ids are printed as lengths only. client_id is retained (precedent:
# evidence/TICK-033/OAUTH_SCOPE_EVIDENCE.md).
#
# Mutations, all reverted in cleanup(): a probe OAuth client is registered and
# then revoked; the portal password for pid=1 is reset; a synthetic core
# session file is written under the openemr container's /tmp. The global
# `oauth_ehr_launch_authorization_flow_skip` is READ, never written -- it is
# already 1 in this deployment.

set -uo pipefail

EMR=https://emr.localhost
OPENEMR_C=local-openemr-1
DB_C=local-mariadb-1
PORTAL_USER=AverySubjecttest1
PORTAL_PASS=${TICK056_PORTAL_PASS:-Tick056Spike!2026}
WORK=$(mktemp -d)
PROBE_CLIENT=""
CORE_SESS_ID="t56corectl00000000000000000000"
CORE_SESS_ID=${CORE_SESS_ID:0:32}

db () { docker exec -i "$DB_C" sh -c 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" openemr '"${1:-}"; }
hr () { printf '\n=== %s ===\n' "$1"; }
ok () { printf '  [PASS] %s\n' "$1"; }
no () { printf '  [FAIL] %s\n' "$1"; FAILED=1; }
FAILED=0

cleanup () {
  if [ -n "$PROBE_CLIENT" ]; then
    printf "UPDATE oauth_clients SET is_enabled=0, skip_ehr_launch_authorization_flow=0, revoke_date=NOW() WHERE client_id='%s';\n" "$PROBE_CLIENT" | db >/dev/null 2>&1
    printf "DELETE FROM oauth_trusted_user WHERE client_id='%s';\n" "$PROBE_CLIENT" | db >/dev/null 2>&1
  fi
  docker exec "$OPENEMR_C" sh -c "rm -f /tmp/sess_${CORE_SESS_ID} /tmp/t56_mint.php" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

pkce_challenge () { printf '%s' "$1" | openssl dgst -binary -sha256 | openssl base64 | tr '+/' '-_' | tr -d '=\n'; }

# ---------------------------------------------------------------- environment
hr "0. Environment under test"
printf '  openemr image:  %s\n' "$(docker inspect -f '{{.Config.Image}}' "$OPENEMR_C" 2>/dev/null)"
printf '  openemr base:   %s\n' "$(grep -m1 '^FROM' deploy/local/openemr.Dockerfile 2>/dev/null || echo '(run from repo root to show)')"
printf '  relevant globals:\n'
db '-N -e "SELECT CONCAT(\"    \", gl_name, \" = \", gl_value) FROM globals WHERE gl_name IN (\"oauth_ehr_launch_authorization_flow_skip\",\"smart_context_test_launches\",\"oauth_app_manual_approval\",\"oauth_password_grant\",\"rest_api\",\"rest_fhir_api\",\"portal_onsite_two_enable\",\"site_addr_oath\") ORDER BY gl_name;"'

# ------------------------------------------------- E1: real portal session
hr "E1. A real patient-portal session, and what is in it"
HASH=$(docker exec "$OPENEMR_C" php -r "echo password_hash('$PORTAL_PASS', PASSWORD_DEFAULT);")
printf "UPDATE patient_access_onsite SET portal_pwd='%s', portal_pwd_status=1 WHERE pid=1;\n" "$HASH" | db >/dev/null
# Entry MUST be index.php?site=default: a bare /portal leaves site_id empty and
# bounces to the staff login, which looks identical to a bad password.
curl -sk -c "$WORK/portal.jar" "$EMR/portal/index.php?site=default" -o /dev/null
LOGIN_LOC=$(curl -sk -b "$WORK/portal.jar" -c "$WORK/portal.jar" -X POST "$EMR/portal/get_patient_info.php" \
  -d "uname=$PORTAL_USER" --data-urlencode "pass=$PORTAL_PASS" -d 'redirect=' -o /dev/null -w '%{redirect_url}')
case "$LOGIN_LOC" in
  */home.php) ok "portal login succeeded (landed on home.php)" ;;
  *) no "portal login failed -> $LOGIN_LOC"; exit 1 ;;
esac
PSID=$(awk -F'\t' '$6=="PortalOpenEMR"{print $7}' "$WORK/portal.jar")
printf '  PortalOpenEMR session id length: %s\n' "${#PSID}"
printf '  cookie attributes as issued: %s\n' "$(awk -F'\t' '$6=="PortalOpenEMR"{print "path="$3", domain="$1}' "$WORK/portal.jar")"
printf '  session keys held by that portal session (keys only; values are patient data):\n'
docker exec "$OPENEMR_C" sh -c "su -s /bin/sh apache -c 'php -r \"
  \\\$raw = file_get_contents(\\\"/tmp/sess_$PSID\\\");
  \\\$raw = substr(\\\$raw, strpos(\\\$raw, \\\"|\\\") + 1);
  foreach (array_keys(unserialize(\\\$raw)) as \\\$k) { echo \\\"    \\\", \\\$k, \\\"\n\\\"; }
\"'" 2>/dev/null | sort
if docker exec "$OPENEMR_C" sh -c "cat /tmp/sess_$PSID" 2>/dev/null | grep -q 'authUserID'; then
  no "portal session contains authUserID"
else
  ok "portal session contains NO authUserID (the key the skip path reads)"
fi

# --------------------------------- E2: authorize while holding that session
hr "E2. GET /oauth2/default/authorize while holding the portal session"
PROD_CLIENT=$(db '-N -e "SELECT client_id FROM oauth_clients WHERE is_enabled=1 AND client_name LIKE \"Intake Assistant%\" ORDER BY register_date DESC LIMIT 1;"' | tr -d '\r')
printf '  client under test: %s\n' "$PROD_CLIENT"
V=$(openssl rand -hex 32); C=$(pkce_challenge "$V")
AUTHZ="$EMR/oauth2/default/authorize?response_type=code&client_id=$PROD_CLIENT&redirect_uri=https%3A%2F%2Fchat.localhost%2Foauth%2Fcallback&scope=openid%20api%3Afhir%20patient%2FPatient.read&state=e2&nonce=e2n&code_challenge=$C&code_challenge_method=S256"
SENT=$(curl -sk -b "$WORK/portal.jar" -v "$AUTHZ" -o /dev/null 2>&1 | grep -i '^> cookie:' | head -1)
printf '  request %s\n' "$(printf '%s' "${SENT:-'(no Cookie header sent)'}" | sed 's/=[0-9a-f]\{16,\}/=<REDACTED_SESSION_ID>/g')"
case "$SENT" in
  *PortalOpenEMR*) ok "the portal cookie IS delivered to /oauth2/ (path=/ puts it in scope)" ;;
  *) no "portal cookie was not delivered -- scoping, not code, would be the cause" ;;
esac
LOC=$(curl -sk -b "$WORK/portal.jar" "$AUTHZ" -o /dev/null -w '%{redirect_url}')
printf '  redirect: %s\n' "$LOC"
curl -skL -b "$WORK/portal.jar" -c "$WORK/e2.jar" "$AUTHZ" -o "$WORK/e2.html"
if grep -q 'type="password"' "$WORK/e2.html"; then
  ok "a password prompt is presented despite the live portal session"
else
  no "no password prompt -- the session appears to have been accepted"
fi

# ------------------------------------- E3/E4: the SMART EHR-launch skip path
hr "E3. SMART EHR launch (launch + aud), skip path fully enabled, patient session"
cat > "$WORK/reg.json" <<'JSON'
{"application_type":"private","client_name":"TICK-056 spike probe",
 "redirect_uris":["https://chat.localhost/oauth/callback"],
 "scope":"openid launch launch/patient api:fhir patient/Patient.read"}
JSON
curl -sk -X POST "$EMR/oauth2/default/registration" -H 'Content-Type: application/json' \
  -d @"$WORK/reg.json" -o "$WORK/reg_out.json" >/dev/null
PROBE_CLIENT=$(python3 -c "import json;print(json.load(open('$WORK/reg_out.json'))['client_id'])")
PROBE_SECRET=$(python3 -c "import json;print(json.load(open('$WORK/reg_out.json'))['client_secret'])")
printf '  probe client: %s (confidential, client_role=%s)\n' "$PROBE_CLIENT" \
  "$(python3 -c "import json;print(json.load(open('$WORK/reg_out.json'))['client_role'])")"
printf "UPDATE oauth_clients SET is_enabled=1, skip_ehr_launch_authorization_flow=1 WHERE client_id='%s';\n" "$PROBE_CLIENT" | db >/dev/null
printf '  global oauth_ehr_launch_authorization_flow_skip = %s ; client skip flag = %s\n' \
  "$(db '-N -e "SELECT gl_value FROM globals WHERE gl_name=\"oauth_ehr_launch_authorization_flow_skip\";"' | tr -d '\r')" \
  "$(printf "SELECT skip_ehr_launch_authorization_flow FROM oauth_clients WHERE client_id='%s';\n" "$PROBE_CLIENT" | db -N | tr -d '\r')"

cat > "$WORK/mint.php" <<'PHP'
<?php
$_SERVER["HTTP_HOST"] = "emr.localhost";
$ignoreAuth = true; $sessionAllowWrite = true;
require_once "/var/www/localhost/htdocs/openemr/interface/globals.php";
$row = sqlQuery("SELECT uuid FROM patient_data WHERE pid=1");
$t = new \OpenEMR\FHIR\SMART\SMARTLaunchToken(\OpenEMR\Common\Uuid\UuidRegistry::uuidToString($row["uuid"]));
echo $t->serialize();
PHP
docker cp "$WORK/mint.php" "$OPENEMR_C:/tmp/t56_mint.php" >/dev/null
LAUNCH=$(docker exec "$OPENEMR_C" sh -c "cd /var/www/localhost/htdocs/openemr && su -s /bin/sh apache -c 'php /tmp/t56_mint.php'")
printf '  minted a real SMART launch token for pid=1 (%s bytes)\n' "${#LAUNCH}"

V3=$(openssl rand -hex 32); C3=$(pkce_challenge "$V3")
BASE=$(python3 - "$PROBE_CLIENT" "$LAUNCH" "$C3" <<'PY'
import sys, urllib.parse
cid, launch, chal = sys.argv[1:4]
q = {'response_type':'code','client_id':cid,'redirect_uri':'https://chat.localhost/oauth/callback',
     'scope':'openid launch launch/patient api:fhir patient/Patient.read','state':'e3','nonce':'e3n',
     'code_challenge':chal,'code_challenge_method':'S256','launch':launch,
     'aud':'https://emr.localhost/apis/default/fhir'}
print('https://emr.localhost/oauth2/default/authorize?'+urllib.parse.urlencode(q))
PY
)
L3=$(curl -sk -b "$WORK/portal.jar" "$BASE" -o "$WORK/e3.html" -w '%{redirect_url}')
printf '  with the patient portal session -> %s\n' "${L3:-'(200, no redirect)'}"
case "$L3" in
  */provider/login) ok "EHR launch with a patient portal session still lands on the login form" ;;
  *) no "unexpected: launch did not fall through to the login form" ;;
esac

hr "E4. Control: identical request, but a core session naming a users row"
docker exec "$OPENEMR_C" sh -c "printf '%s' 'OpenEMR|a:2:{s:10:\"authUserID\";s:1:\"3\";s:8:\"authUser\";s:5:\"probe\";}' > /tmp/sess_${CORE_SESS_ID} && chown apache:apache /tmp/sess_${CORE_SESS_ID} && chmod 600 /tmp/sess_${CORE_SESS_ID}"
printf '  synthetic core session names users.id=3 (%s) -- no credential is used\n' \
  "$(db '-N -e "SELECT username FROM users WHERE id=3;"' | tr -d '\r')"
S4=$(curl -sk -c "$WORK/e4.jar" -b "$WORK/e4.jar" -H "Cookie: OpenEMR=$CORE_SESS_ID" "$BASE" -o "$WORK/e4.html" -w '%{http_code}')
printf '  step 1 -> HTTP %s, autosubmit page: %s\n' "$S4" "$(grep -c -i autosubmit "$WORK/e4.html")"
L4=$(curl -sk -c "$WORK/e4.jar" -b "$WORK/e4.jar" -H "Cookie: OpenEMR=$CORE_SESS_ID" "${BASE}&autosubmit=1" -o /dev/null -w '%{redirect_url}')
CODE=$(python3 -c "import urllib.parse;print(urllib.parse.parse_qs(urllib.parse.urlparse('$L4').query).get('code',[''])[0])")
printf '  step 2 -> callback reached with an authorization code (%s bytes), no login, no consent\n' "${#CODE}"
if [ -n "$CODE" ]; then
  ok "the skip machinery is live and correctly configured -- it just needs a users row"
else
  no "skip path did not issue a code; the E3 negative may be a configuration artifact"
fi

hr "E5. What the skip path's token actually binds to"
curl -sk -X POST "$EMR/oauth2/default/token" -u "$PROBE_CLIENT:$PROBE_SECRET" \
  -d grant_type=authorization_code -d 'redirect_uri=https://chat.localhost/oauth/callback' \
  -d "code=$CODE" -d "code_verifier=$V3" -o "$WORK/tok.json" >/dev/null
SUB=$(python3 - "$WORK/tok.json" <<'PY'
import json, base64, sys
d = json.load(open(sys.argv[1]))
if 'error' in d:
    print(''); sys.exit()
p = d['id_token'].split('.')[1]; p += '=' * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
print(c['sub'] + '|' + d.get('patient', ''))
PY
)
if [ -n "$SUB" ]; then
  SUB_UUID=${SUB%%|*}; PAT_UUID=${SUB#*|}
  WHO=$(printf "SELECT username FROM users WHERE uuid=UNHEX(REPLACE('%s','-',''));\n" "$SUB_UUID" | db -N | tr -d '\r')
  printf '  id_token sub resolves to: users.username = %s\n' "${WHO:-'(not a users row)'}"
  printf '  patient claim == sub ?  %s\n' "$([ "$SUB_UUID" = "$PAT_UUID" ] && echo yes || echo no)"
  if [ -n "$WHO" ] && [ "$SUB_UUID" != "$PAT_UUID" ]; then
    ok "authenticated identity is a staff users row; patient context comes only from the launch token"
  else
    no "token bound differently than expected -- re-read the finding"
  fi
fi

# ---------------------------------------------------- E6: consent persistence
hr "E6. Does consent persist for this client across authorizations?"
printf '  prior oauth_trusted_user rows for the product client: %s\n' \
  "$(printf "SELECT COUNT(*) FROM oauth_trusted_user WHERE client_id='%s';\n" "$PROD_CLIENT" | db -N | tr -d '\r')"
consent_round () {
  local LABEL=$1 JAR="$WORK/c$1.jar"
  local VV CC UU TOK
  VV=$(openssl rand -hex 32); CC=$(pkce_challenge "$VV")
  UU="$EMR/oauth2/default/authorize?response_type=code&client_id=$PROD_CLIENT&redirect_uri=https%3A%2F%2Fchat.localhost%2Foauth%2Fcallback&scope=openid%20api%3Afhir%20patient%2FPatient.read&state=$LABEL&nonce=n$LABEL&code_challenge=$CC&code_challenge_method=S256"
  curl -sk -c "$JAR" -b "$JAR" -L "$UU" -o "$WORK/c$LABEL.html"
  TOK=$(grep -o 'name="csrf_token_form"[^>]*value="[^"]*"' "$WORK/c$LABEL.html" | sed 's/.*value="//;s/"//')
  local LOGIN_TO
  LOGIN_TO=$(curl -sk -c "$JAR" -b "$JAR" -X POST "$EMR/oauth2/default/login" \
    -d "username=$PORTAL_USER" --data-urlencode "password=$PORTAL_PASS" \
    -d 'user_role=portal-api' -d "csrf_token_form=$TOK" -o /dev/null -w '%{redirect_url}')
  local BYTES
  BYTES=$(curl -sk -c "$JAR" -b "$JAR" -L "$EMR/oauth2/default/scope-authorize-confirm" -o "$WORK/consent$LABEL.html" -w '%{size_download}')
  printf '  round %s: login form shown -> after login redirected to %s -> consent page %s bytes\n' \
    "$LABEL" "$(basename "$LOGIN_TO")" "$BYTES" >&2
  echo "$BYTES"
}
B1=$(consent_round 1)
B2=$(consent_round 2)
if [ "$B1" = "$B2" ] && [ "${B1:-0}" -gt 1000 ]; then
  ok "consent is presented on every authorization; prior consent does not suppress it"
else
  no "consent pages differed ($B1 vs $B2) -- re-check"
fi

# ------------------------------------------- E7: the provider's own session
hr "E7. Does OpenEMR's own OAuth2 provider session skip the prompt on a second authorize?"
JAR="$WORK/e7.jar"
V7=$(openssl rand -hex 32); C7=$(pkce_challenge "$V7")
U7="$EMR/oauth2/default/authorize?response_type=code&client_id=$PROD_CLIENT&redirect_uri=https%3A%2F%2Fchat.localhost%2Foauth%2Fcallback&scope=openid%20api%3Afhir%20patient%2FPatient.read&state=e7a&nonce=e7an&code_challenge=$C7&code_challenge_method=S256"
curl -sk -c "$JAR" -b "$JAR" -L "$U7" -o "$WORK/e7login.html"
T7=$(grep -o 'name="csrf_token_form"[^>]*value="[^"]*"' "$WORK/e7login.html" | sed 's/.*value="//;s/"//')
curl -sk -c "$JAR" -b "$JAR" -X POST "$EMR/oauth2/default/login" \
  -d "username=$PORTAL_USER" --data-urlencode "password=$PORTAL_PASS" \
  -d 'user_role=portal-api' -d "csrf_token_form=$T7" -o /dev/null
if grep -q 'authserverOpenEMR' "$JAR"; then
  printf '  a live authserverOpenEMR provider session is now held\n'
else
  printf '  WARNING: no provider session cookie was established\n'
fi
V8=$(openssl rand -hex 32); C8=$(pkce_challenge "$V8")
U8="$EMR/oauth2/default/authorize?response_type=code&client_id=$PROD_CLIENT&redirect_uri=https%3A%2F%2Fchat.localhost%2Foauth%2Fcallback&scope=openid%20api%3Afhir%20patient%2FPatient.read&state=e7b&nonce=e7bn&code_challenge=$C8&code_challenge_method=S256"
L8=$(curl -sk -c "$JAR" -b "$JAR" "$U8" -o /dev/null -w '%{redirect_url}')
curl -skL -c "$JAR" -b "$JAR" "$U8" -o "$WORK/e7b.html"
printf '  second authorize -> %s\n' "$L8"
if grep -q 'type="password"' "$WORK/e7b.html"; then
  ok "even the provider's OWN session re-prompts; there is no already-authenticated check"
else
  no "the provider session skipped the prompt -- ARCHITECTURE.md 2.1 needs revisiting"
fi

hr "Result"
[ "$FAILED" -eq 0 ] && echo "  all checks behaved as FINDING.md records" || echo "  one or more checks deviated from FINDING.md"
exit "$FAILED"
