#!/bin/sh
# Container entrypoint for Personal Finance Analyzer.
#
#   pfa-entrypoint serve [pfa serve options]   migrate the database, then run the web server
#   pfa-entrypoint <anything else>             run that command (e.g. `pfa backup create ...`)
#   pfa-entrypoint run <command...>            run a command as PUID:PGID with UMASK, no setup
#                                              (what the `pfa` wrapper uses for `docker exec`)
#
# Environment (same names as the linuxserver.io images, so a NAS user's habits carry over):
#   PUID, PGID  numeric user and group that own the files on the bind mounts (default 1000:1000).
#               Use the NAS user's ids (`id <user>` over SSH) so you can read and edit the files.
#   UMASK       permission mask for files the app creates (default 077: owner only; the app
#               itself keeps its directories 0700 whatever this says).
#   PFA_HOME    the data directory (default /pfa/data).
#
# Started as root it fixes ownership of the data directory, then drops to PUID:PGID with setpriv
# (util-linux, already in the image) before anything of the app runs. Started with
# `--user UID:GID` it cannot change ownership and simply checks it can write.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-077}"
DATA="${PFA_HOME:-/pfa/data}"
PARENT="$(dirname "$DATA")"

say() { printf 'pfa-entrypoint: %s\n' "$*" >&2; }
die() { say "ERROR: $*"; exit 1; }
is_number() { case "$1" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }
owner_of() { stat -c '%u:%g' "$1"; }

case "$UMASK" in
  [0-7][0-7][0-7] | 0[0-7][0-7][0-7]) ;;
  *) die "UMASK must be an octal mask such as 077, 027 or 022 (got '$UMASK')" ;;
esac

if [ "$(id -u)" = 0 ] && [ -z "${PFA_ENTRYPOINT_DROPPED:-}" ]; then
  is_number "$PUID" && is_number "$PGID" || die "PUID and PGID must be numbers (got '$PUID' and '$PGID')"
  if [ "$PUID" = 0 ] && [ "${PFA_ALLOW_ROOT:-}" != 1 ]; then
    die "PUID=0 would run the app as root. Use your NAS user's ids (see 'id <user>'), or set PFA_ALLOW_ROOT=1 if you really mean it."
  fi

  if [ "${1:-serve}" != run ]; then
    # Docker creates a missing host directory root-owned: hand the data directory to the app user.
    # Recursive only when the top directory is not already theirs (first start, or PUID changed),
    # so a restart never walks thousands of PDFs. Best effort: NFS with root squash refuses
    # chown, and that is fine as long as the app user can write (checked below).
    umask 077
    mkdir -p "$DATA" 2>/dev/null || true
    if [ "$(owner_of "$PARENT")" != "$PUID:$PGID" ]; then
      chown "$PUID:$PGID" "$PARENT" 2>/dev/null || say "note: could not chown $PARENT to $PUID:$PGID"
    fi
    if [ -d "$DATA" ] && [ "$(owner_of "$DATA")" != "$PUID:$PGID" ]; then
      say "setting ownership of $DATA to $PUID:$PGID (first start or changed PUID/PGID)"
      chown -R "$PUID:$PGID" "$DATA" 2>/dev/null || say "note: could not chown everything under $DATA"
    fi
  fi

  PFA_ENTRYPOINT_DROPPED=1 exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$0" "$@"
fi

# From here on we are the unprivileged app user (or whoever `--user` named).
umask "$UMASK"

# Scratch space for large temporary files, beside the data on disk rather than in /tmp, which is
# often a RAM-backed tmpfs on a NAS. (Backup exports and restore uploads, which can be as large as
# all your PDFs, do not use it: the app keeps those in private work directories inside and beside
# the data directory, on the same disk, and sweeps away what a crash leaves behind.)
TMPDIR="${TMPDIR:-$PARENT/tmp}"
export TMPDIR
mkdir -p "$TMPDIR" 2>/dev/null && chmod 700 "$TMPDIR" 2>/dev/null || true

if [ "${1:-serve}" = run ]; then
  shift
  exec "$@"
fi

mkdir -p "$DATA" 2>/dev/null || true
if [ ! -d "$DATA" ] || [ ! -w "$DATA" ] || [ ! -w "$PARENT" ]; then
  die "cannot write to $DATA and $PARENT as uid $(id -u) gid $(id -g). Set PUID/PGID to the owner of the host directory mounted at $PARENT, or make that directory writable for uid $(id -u)."
fi

# A missing bind mount is the one way to lose data: the files would live in the container's own
# layer and vanish with `docker rm` / an image update. Say so, loudly.
if ! awk -v a="$DATA" -v b="$PARENT" '$5 == a || $5 == b { found = 1 } END { exit !found }' /proc/self/mountinfo 2>/dev/null; then
  say "WARNING: nothing is mounted at $PARENT. Your data is stored inside the container and will be LOST when the container is removed or recreated. Bind-mount a host directory there (see docker-compose.yml)."
fi

case "${1:-serve}" in
  serve)
    [ "$#" -gt 0 ] && shift
    if [ "$TMPDIR" = "$PARENT/tmp" ]; then # leftovers of an interrupted run
      rm -rf "${PARENT:?}/tmp"/* 2>/dev/null || true
    fi
    say "uid=$(id -u) gid=$(id -g) umask=$UMASK data=$DATA"
    /opt/pfa/bin/pfa migrate
    exec /opt/pfa/bin/pfa serve --no-open-browser "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
