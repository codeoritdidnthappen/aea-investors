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

if [ "$fail" -ne 0 ]; then
    echo "VERIFY_STACK_FAILED" >&2
    exit 1
fi
echo "VERIFY_STACK_OK"
