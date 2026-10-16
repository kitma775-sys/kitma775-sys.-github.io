#!/bin/sh
# Volumes are mounted root-owned: take ownership of the data dir, then drop to the
# unprivileged user. If the platform forbids either step, keep running rather than
# lose the bot, and say so in the log.
set -e

if [ "$(id -u)" = "0" ]; then
    data="${POLYARB_DATA_DIR:-/data}"
    mkdir -p "$data"
    chown -R polyarb:polyarb "$data" 2>/dev/null || echo "entrypoint: cannot chown $data" >&2
    drop="setpriv --reuid=polyarb --regid=polyarb --init-groups"
    if command -v setpriv >/dev/null 2>&1 && $drop test -w "$data" 2>/dev/null; then
        exec $drop "$@"
    fi
    echo "entrypoint: running as root ($data not writable for polyarb or setpriv unavailable)" >&2
fi

exec "$@"
