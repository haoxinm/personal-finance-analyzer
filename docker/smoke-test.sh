#!/bin/sh
# shellcheck disable=SC2016  # the "$1" in inmount scripts is meant to expand inside the container
# Smoke test for a built image (run by CI after the build; runnable by hand):
#
#   docker/smoke-test.sh [image]            # default image: personal-finance-analyzer:ci
#   PFA_SMOKE_CHECK_OWNER=1 docker/smoke-test.sh img   # also assert PUID/PGID ownership on the
#                                                      # bind mount (real Linux filesystems only:
#                                                      # Docker Desktop/OrbStack remap owners)
#
# Starts the container with bind mounts under a scratch directory, a password file and a PUID/PGID,
# then checks: healthy; the login is required; a wrong password and a right one; migrations ran
# (the API answers with the default user); the data landed on the bind mount; an uploaded PDF is
# kept at a readable path (./pfa/documents/<Person>/Statements/...) on the host, with the README,
# and no hash-named statements/ folder; and a recreated container finds the same data and file.
# Everything it creates is removed on exit.
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
PERSON_ID="$(api -X POST -H 'Content-Type: application/json' -d '{"name":"Persisted"}' "$URL/api/users" \
  | sed -n 's/.*"id":\([0-9][0-9]*\).*/\1/p')"
[ -n "$PERSON_ID" ] || fail "could not create a person"

# A one-page PDF with no text layer: the app files it straight away (nothing to parse), so the
# readable path can be checked without a real statement. (A real import files it the same way
# once the statement is confirmed, as <Person>/Statements/<Bank>/<YYYY-MM> <Bank> <account>.pdf.)
printf '%s\n' '%PDF-1.4' \
  '1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj' \
  '2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj' \
  '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj' \
  'trailer<</Root 1 0 R/Size 4>>' '%%EOF' > "$WORK/blank.pdf"
check "upload accepted" "$(status -b "$JAR" -H "X-PFA-Token: $PAGE_TOKEN" -F "user_id=$PERSON_ID" \
  -F "file=@$WORK/blank.pdf;type=application/pdf" "$URL/api/statements")" 201

inmount 'test -f /pfa/data/finance.db' || fail "finance.db is not on the bind mount"
echo "ok   finance.db is on the bind mount"

# The PDF is on the host under a readable path (not data/statements/<hash>.pdf). Looked at from
# inside the mount (paths below are the container's): the documents folder is 0700 PUID:PGID.
UNSORTED=/pfa/documents/Persisted/Statements/Unsorted
KEPT="$(inmount 'find "$1" -type f -name "*.pdf" 2>/dev/null' "$UNSORTED")" || KEPT=""
[ -n "$KEPT" ] && [ "$(printf '%s\n' "$KEPT" | wc -l | tr -d ' ')" = 1 ] \
  || fail "the uploaded PDF is not at $UNSORTED/<date> Statement <hash>.pdf"
case "$(basename "$KEPT")" in
  *' Statement '*.pdf) ;;
  *) fail "unexpected document name: $(basename "$KEPT")" ;;
esac
echo "ok   uploaded PDF is on the host at documents/Persisted/Statements/Unsorted/$(basename "$KEPT")"
inmount 'test -f /pfa/documents/README.txt' || fail "documents/README.txt is missing"
echo "ok   documents/README.txt explains the layout"
inmount 'test ! -e /pfa/data/statements' || fail "data/statements/ exists: PDFs must not be kept by hash there any more"
[ -z "$(inmount 'find /pfa/data/incoming -name "*.pdf" 2>/dev/null')" ] || fail "an upload was left in data/incoming"
echo "ok   no hash-named copies under data/"
if [ "${PFA_SMOKE_CHECK_OWNER:-}" = 1 ]; then
  check "files are owned by PUID:PGID" "$(inmount 'stat -c %u:%g /pfa/data/finance.db')" "$PUID_:$PGID_"
  check "documents folder is owned by PUID:PGID" "$(inmount 'stat -c %u:%g /pfa/documents')" "$PUID_:$PGID_"
  check "kept PDF is owned by PUID:PGID" "$(inmount 'stat -c %u:%g "$1"' "$KEPT")" "$PUID_:$PGID_"
  check "kept PDF is private by default (UMASK 077)" "$(inmount 'stat -c %a "$1"' "$KEPT")" 600
fi

docker rm -f "$NAME" >/dev/null
start
login
check "data survives recreating the container" "$(api "$URL/api/users" | grep -c Persisted)" 1
inmount 'test -f "$1"' "$KEPT" || fail "the kept PDF disappeared after recreating the container"
echo "ok   the kept PDF is still on the host"

echo "SMOKE TEST PASSED"
