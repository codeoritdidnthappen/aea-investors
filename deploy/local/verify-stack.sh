#!/usr/bin/env sh
# TICK-057: assert the AI Chat panel can actually reach a patient.
#
# Run after `docker compose up` and after enabling the module (README step 4).
# The preflight covers what is checkable before the stack starts; this covers the
# two states that are only observable once it is running, both of which have now
# removed the chat from the portal with nothing reporting a fault:
#
#   1. The module directory mounted empty, because its source was deleted.
#   2. The module present on disk but `mod_active = 0`, so OpenEMR never loads it
#      -- no hook fires, no tile renders, and every file check still passes.
#
# The second is deliberately NOT in the container healthcheck: an administrator
# may disable the module on purpose, and that is not a broken container.
set -eu

cd "$(dirname "$0")"
MODULE_PATH=/var/www/localhost/htdocs/openemr/interface/modules/custom_modules/aeai-portal-chat
CONTROLLER="$MODULE_PATH/src/Controller/PortalChatController.php"
fail=0

note() { printf '  %s\n' "$1"; }

if ! docker compose ps --status running openemr >/dev/null 2>&1; then
    echo "VERIFY_STACK_FAILED: openemr is not running" >&2
    exit 1
fi

# 1. The module actually arrived in the container, and matches the host.
if ! docker compose exec -T openemr test -s "$CONTROLLER" 2>/dev/null; then
    note "module controller missing or empty inside the container ($CONTROLLER)"
    note "  the bind-mount source is probably gone; see the preflight"
    fail=1
else
    host_sum=$(shasum -a 256 ../../openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php | cut -d' ' -f1)
    cont_sum=$(docker compose exec -T openemr sha256sum "$CONTROLLER" | cut -d' ' -f1)
    if [ "$host_sum" != "$cont_sum" ]; then
        note "module controller in the container differs from the host copy"
        fail=1
    fi
fi

# 2. OpenEMR is actually loading it. Files present with mod_active = 0 renders
#    nothing at all, which is what a patient sees as "the chat tile is gone".
active=$(docker compose exec -T mariadb sh -lc \
    'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" openemr -N -B -e \
     "select mod_active from modules where mod_directory='"'"'aeai-portal-chat'"'"';"' 2>/dev/null || true)
case "$active" in
    1*) : ;;
    0*) note "the module is registered but mod_active = 0, so OpenEMR never loads it"
        note "  no hook fires and no tile renders -- enable it (README step 4)"
        fail=1 ;;
    *)  note "no modules row for aeai-portal-chat: it has not been registered yet"
        note "  register and install it (README step 4)"
        fail=1 ;;
esac

# --- TICK-059: the local model server ----------------------------------------
#
# A stack that starts and then cannot answer is the failure mode this repo has
# already had twice, so "the container is up" is not what gets checked here. The
# three states below all leave `docker compose ps` looking fine:
#
#   3. The model server is not running at all, so every chat turn degrades to the
#      unavailable message (LOCAL_LLM_SPEC D12) -- honest, but a fault.
#   4. It is running but holds no model, or holds a *different* model than the one
#      pinned. Cold-volume pulls take a long time and can be interrupted.
#   5. The AI server cannot resolve it by service name -- the one thing that is
#      invisible from either container on its own, and the failure a host-address
#      default (`localhost:11434`) produces on every machine but a laptop.

# `--quiet` and a test for *output*, not for exit status: `docker compose ps --status
# running <svc>` exits 0 whether or not anything matched (confirmed live, see
# evidence/TICK-059), so testing its exit status alone is a check that never fires.
if [ -z "$(docker compose ps --status running --quiet ollama 2>/dev/null)" ]; then
    note "the model server (ollama) is not running"
    note "  every chat turn will report the model unavailable (LOCAL_LLM_SPEC D12)"
    fail=1
else
    # 3/4. The pinned model is present and still hashes to the pinned digest. This
    #      is the container's own healthcheck, run here too so the reason is printed
    #      rather than left as an `unhealthy` flag nobody reads.
    if ! digest_error=$(docker compose exec -T ollama /usr/local/bin/aeai-ollama-healthcheck 2>&1); then
        note "the model server is not able to answer:"
        note "  ${digest_error}"
        note "  a cold volume pulls the model once on first start; if this persists,"
        note "  check \`docker compose logs ollama\` for a digest mismatch"
        fail=1
    fi

    # 5. The AI server reaches it *by service name*. Run from inside the ai-server
    #    container on purpose: this is the only check that proves the two are on a
    #    shared network under the name the AI server is actually configured with.
    if [ -n "$(docker compose ps --status running --quiet ai-server 2>/dev/null)" ]; then
        # tr: strip any stray CR/LF so the URL is not silently unusable below.
        ollama_host=$(docker compose exec -T ai-server printenv OLLAMA_HOST 2>/dev/null \
            | tr -d '\r\n' || true)
        if [ -z "$ollama_host" ]; then
            note "the ai-server container has no OLLAMA_HOST set"
            note "  it would fall back to http://localhost:11434, which inside that"
            note "  container is the AI server itself, not the model server"
            fail=1
        elif ! docker compose exec -T ai-server \
            python -c "import sys,urllib.request;urllib.request.urlopen(sys.argv[1]+'/api/tags',timeout=10)" \
            "$ollama_host" >/dev/null 2>&1; then
            note "the ai-server cannot reach the model server at $ollama_host"
            note "  the chat would report the model unavailable while both containers"
            note "  look healthy; check they share the \`app\` network"
            fail=1
        fi
    fi
fi

if [ "$fail" -ne 0 ]; then
    echo "VERIFY_STACK_FAILED" >&2
    exit 1
fi
echo "VERIFY_STACK_OK"
