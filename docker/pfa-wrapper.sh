#!/bin/sh
# `pfa` inside the container. `docker exec` starts as root regardless of PUID/PGID, and a backup or
# an import made as root would leave root-owned files on the bind mount; so as root, hand over to
# the entrypoint, which runs the real command as PUID:PGID with UMASK. As anyone else, just run it.
if [ "$(id -u)" = 0 ]; then
  exec /usr/local/bin/pfa-entrypoint run /opt/pfa/bin/pfa "$@"
fi
exec /opt/pfa/bin/pfa "$@"
