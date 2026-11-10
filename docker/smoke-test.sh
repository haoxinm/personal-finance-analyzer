#!/bin/sh
# Smoke test for a built image (run by CI after the build; runnable by hand):
#
#   docker/smoke-test.sh [image]            # default image: personal-finance-analyzer:ci
#   PFA_SMOKE_CHECK_OWNER=1 docker/smoke-test.sh img   # also assert PUID/PGID ownership on the
#                                                      # bind mount (real Linux filesystems only:
#                                                      # Docker Desktop/OrbStack remap owners)
#
# Starts the container with bind mounts under a scratch directory, a password file and a PUID/PGID,
# then checks: healthy; the login is required; a wrong password and a right one; migrations ran
# (the API answers with the default user); the data landed on the bind mount; and a recreated
# container finds the same data. Everything it creates is removed on exit.
#
# The container keeps the data under PUID:PGID with mode 0700/0600 (UMASK 077), so the user running
# this script (a CI runner is uid 1001, never the PUID) usually cannot even list the bind mount.
# Everything the script looks at inside it goes through inmount(), a throwaway root container.
set -eu

IMAGE="${1:-personal-finance-analyzer:ci}"
PORT="${PFA_SMOKE_PORT:-18756}"
NAME="pfa-smoke-$$"
PUID_="${PFA_SMOKE_PUID:-1234}"
PGID_="${PFA_SMOKE_PGID:-5678}"
PASSWORD="smoke-test-password"
WORK="$(mktemp -d)"
URL="http://localhost:$PORT"
JAR="$WORK/jar.txt"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # The container created files as PUID:PGID; remove them from inside a container so this also
  # works when the invoking user is not that uid.
  docker run --rm -v "$WORK:/w" --entrypoint rm "$IMAGE" -rf /w/pfa >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

# inmount SCRIPT [ARG...]: run an `sh -c SCRIPT` as root in a throwaway container with the bind
# mount at /pfa, and pass its exit status and output on. The image has no USER, so --entrypoint
# bypasses the privilege drop and root can read what is owned by PUID:PGID. The host-side path
# would only work when the invoking user happens to own the files (and `stat -c` is GNU-only,
# which macOS is not; the one in the image is).
inmount() {
  _script="$1"
  shift
  docker run --rm --network none -v "$WORK/pfa:/pfa" --entrypoint sh "$IMAGE" -c "$_script" inmount "$@"
}

fail() { echo "SMOKE TEST FAILED: $*" >&2; docker logs "$NAME" 2>&1 | tail -20 >&2 || true; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"; echo "ok   $1"; }
status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

mkdir -p "$WORK/secrets"
printf '%s' "$PASSWORD" > "$WORK/secrets/pfa_password"
chmod 644 "$WORK/secrets/pfa_password"

start() {
  docker run -d --name "$NAME" --init -p "127.0.0.1:$PORT:8756" \
    -e PUID="$PUID_" -e PGID="$PGID_" -e PFA_ALLOWED_HOSTS=localhost \
    -e PFA_PASSWORD_FILE=/run/secrets/pfa_password \
    -v "$WORK/pfa:/pfa" -v "$WORK/secrets/pfa_password:/run/secrets/pfa_password:ro" \
    --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add DAC_OVERRIDE \
    --security-opt no-new-privileges:true \
    "$IMAGE" >/dev/null
  i=0
  while [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" != healthy ]; do
    i=$((i + 1)); [ "$i" -le 40 ] || fail "container did not become healthy"
    sleep 3
  done
}

login() {
  rm -f "$JAR"
  TOKEN="$(curl -s "$URL/login" | sed -n 's/.*name="token" value="\([^"]*\)".*/\1/p')"
  [ -n "$TOKEN" ] || fail "login page has no token"
  curl -s -o /dev/null -c "$JAR" -d "password=$PASSWORD&token=$TOKEN&next=/" "$URL/login"
  PAGE_TOKEN="$(curl -s -b "$JAR" "$URL/" | sed -n 's/.*name="pfa-token" content="\([^"]*\)".*/\1/p' | tail -1)"
}

start
echo "ok   container healthy"
check "health endpoint" "$(curl -s "$URL/api/health")" '{"status":"ok"}'
check "page needs a login" "$(status "$URL/")" 303
check "API needs a login" "$(status "$URL/api/users")" 401
TOKEN="$(curl -s "$URL/login" | sed -n 's/.*name="token" value="\([^"]*\)".*/\1/p')"
check "wrong password refused" "$(status -d "password=wrong-password&token=$TOKEN" "$URL/login")" 401
login
check "SPA served after login" "$(status -b "$JAR" "$URL/")" 200
check "API needs the token too" "$(status -b "$JAR" "$URL/api/users")" 403
api() { curl -s -b "$JAR" -H "X-PFA-Token: $PAGE_TOKEN" "$@"; }
check "migrated: default user exists" "$(api "$URL/api/users" | grep -c '"is_default":true')" 1
api -X POST -H 'Content-Type: application/json' -d '{"name":"Persisted"}' "$URL/api/users" >/dev/null

inmount 'test -f /pfa/data/finance.db' || fail "finance.db is not on the bind mount"
echo "ok   finance.db is on the bind mount"
if [ "${PFA_SMOKE_CHECK_OWNER:-}" = 1 ]; then
  check "files are owned by PUID:PGID" "$(inmount 'stat -c %u:%g /pfa/data/finance.db')" "$PUID_:$PGID_"
fi

docker rm -f "$NAME" >/dev/null
start
login
check "data survives recreating the container" "$(api "$URL/api/users" | grep -c Persisted)" 1

echo "SMOKE TEST PASSED"
