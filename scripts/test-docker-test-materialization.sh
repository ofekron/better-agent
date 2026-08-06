#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib/docker-test-lifecycle.sh"

if command -v flock >/dev/null 2>&1; then
  REAL_LOCKER="$(command -v flock)"
  LOCKER_STYLE=flock
else
  REAL_LOCKER="$(command -v lockf)"
  LOCKER_STYLE=lockf
fi

TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/better-agent-materialize-test.XXXXXX")"
TEST_LOCK_ROOT="/tmp/better-agent-docker-build-locks-materialization-test-$$"
cleanup() {
  if [ -n "${DOCKER_TEST_RUN_ID:-}" ]; then
    docker_test_prepare_lock_root >/dev/null 2>&1 || true
    rm -f "${DOCKER_TEST_LEASE_DIR:-/nonexistent}/$DOCKER_TEST_RUN_ID"
  fi
  rm -rf "$TEST_ROOT"
  rm -rf "$TEST_LOCK_ROOT"
}
trap cleanup EXIT HUP INT TERM
FAKE_BIN="$TEST_ROOT/bin"
STATE="$TEST_ROOT/state"
mkdir -p "$FAKE_BIN" "$STATE"
: > "$STATE/builds"

cat > "$FAKE_BIN/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
safe_tag() { printf '%s' "$1" | tr '/:' '__'; }
case "${1:-} ${2:-}" in
  "context inspect") printf '%s\n' unix:///tmp/fake-docker.sock ;;
  "version --format") printf '%s\n' linux/amd64 ;;
  "buildx inspect") exit 0 ;;
  "image inspect")
    tag="${@: -1}"
    file="$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "$tag")"
    [ -f "$file" ] || exit 1
    cat "$file"
    ;;
  "buildx build")
    tag=""
    fingerprint=""
    previous=""
    for arg in "$@"; do
      if [ "$previous" = "-t" ]; then tag="$arg"; fi
      case "$arg" in
        com.better-agent.test.fingerprint=*) fingerprint="${arg#*=}" ;;
      esac
      previous="$arg"
    done
    printf 'build %s %s\n' "$tag" "$fingerprint" >> "$DOCKER_TEST_FAKE_STATE/builds"
    if [ -n "${DOCKER_TEST_FAKE_STALL_BUILD:-}" ]; then
      printf 'one line of progress, then silence\n'
      exec sleep 6317
    fi
    if [ -n "${DOCKER_TEST_FAKE_BUILD_FIFO:-}" ]; then
      printf 'build-started\n' > "$DOCKER_TEST_FAKE_BUILD_FIFO"
    fi
    if [ -n "${DOCKER_TEST_FAKE_RELEASE_FIFO:-}" ]; then
      IFS= read -r _ < "$DOCKER_TEST_FAKE_RELEASE_FIFO"
    fi
    [ -z "${DOCKER_TEST_FAKE_FAIL_BUILD:-}" ] || exit 17
    printf '%s\n' "$fingerprint" > "$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "$tag")"
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$FAKE_BIN/docker"

cat > "$FAKE_BIN/flock" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ -n "${DOCKER_TEST_FAKE_LOCK_FIFO:-}" ]; then
  printf 'lock-attempted\n' > "$DOCKER_TEST_FAKE_LOCK_FIFO"
fi
if [ "$DOCKER_TEST_REAL_LOCKER_STYLE" = flock ]; then
  exec "$DOCKER_TEST_REAL_LOCKER" "$@"
fi
if [ "${1:-}" = -E ]; then shift 2; fi
if [ "${1:-}" = -n ]; then
  shift
  exec "$DOCKER_TEST_REAL_LOCKER" -s -t 0 -k "$@"
fi
exec "$DOCKER_TEST_REAL_LOCKER" -k "$@"
EOF
chmod +x "$FAKE_BIN/flock"

export PATH="$FAKE_BIN:$PATH"
export DOCKER_TEST_FAKE_STATE="$STATE"
export DOCKER_TEST_REAL_LOCKER="$REAL_LOCKER"
export DOCKER_TEST_REAL_LOCKER_STYLE="$LOCKER_STYLE"
id() {
  [ "${1:-}" = -u ] && printf 'materialization-test-%s\n' "$$"
}
DOCKER_TEST_FAMILY=backend
DOCKER_TEST_BUILDER="better-agent-tests-materialization-$$"
DOCKER_TEST_HOST=test-host
DOCKER_TEST_OWNER_START=test-start
DOCKER_TEST_RUN_ID="materialization-test-$$"
DOCKER_TEST_IMAGE_LEASE=""

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

rm -rf "$TEST_LOCK_ROOT"
mkdir() {
  local target="${@: -1}"
  if [ "$target" = "$TEST_LOCK_ROOT" ] && [ ! -e "$STATE/root-eexist" ]; then
    command mkdir "$@"
    : > "$STATE/root-eexist"
    return 1
  fi
  if [ "$target" = "$TEST_LOCK_ROOT/leases" ] && [ ! -e "$STATE/leases-eexist" ]; then
    command mkdir "$@"
    : > "$STATE/leases-eexist"
    return 1
  fi
  command mkdir "$@"
}
docker_test_prepare_lock_root
unset -f mkdir
[ -d "$TEST_LOCK_ROOT/leases" ] || fail "create-or-validate lost an EEXIST race"

FINGERPRINT="same-key"
IMAGE="better-agent-backend-tests:deps-same-key"
FIFO="$STATE/release"
LOCK_FIFO="$STATE/lock-attempt"
BUILD_FIFO="$STATE/build-start"
mkfifo "$FIFO"
mkfifo "$LOCK_FIFO" "$BUILD_FIFO"
export DOCKER_TEST_FAKE_RELEASE_FIFO="$FIFO"
export DOCKER_TEST_FAKE_LOCK_FIFO="$LOCK_FIFO"
export DOCKER_TEST_FAKE_BUILD_FIFO="$BUILD_FIFO"

docker_test_materialize_image "$FINGERPRINT" "$IMAGE" -t "$IMAGE" . &
first_pid=$!
IFS= read -r _ < "$LOCK_FIFO"
IFS= read -r _ < "$LOCK_FIFO"
IFS= read -r _ < "$BUILD_FIFO"
docker_test_materialize_image "$FINGERPRINT" "$IMAGE" -t "$IMAGE" . &
second_pid=$!
IFS= read -r _ < "$LOCK_FIFO"
IFS= read -r _ < "$LOCK_FIFO"
[ "$(wc -l < "$STATE/builds")" -eq 1 ] || fail "same-key follower started a duplicate export"
printf 'release\n' > "$FIFO"
wait "$first_pid"
wait "$second_pid"
[ "$(wc -l < "$STATE/builds")" -eq 1 ] || fail "same-key materialization was not single-flight"

unset DOCKER_TEST_FAKE_RELEASE_FIFO DOCKER_TEST_FAKE_LOCK_FIFO DOCKER_TEST_FAKE_BUILD_FIFO
docker_test_materialize_image "$FINGERPRINT" "$IMAGE" -t "$IMAGE" .
[ "$(wc -l < "$STATE/builds")" -eq 1 ] || fail "valid warm image rebuilt"

mkfifo "$STATE/release-different"
export DOCKER_TEST_FAKE_RELEASE_FIFO="$STATE/release-different"
export DOCKER_TEST_FAKE_LOCK_FIFO="$LOCK_FIFO"
export DOCKER_TEST_FAKE_BUILD_FIFO="$BUILD_FIFO"
FIRST_DIFFERENT="better-agent-backend-tests:deps-first-different"
SECOND_DIFFERENT="better-agent-backend-tests:deps-second-different"
docker_test_materialize_image first-different "$FIRST_DIFFERENT" -t "$FIRST_DIFFERENT" . &
first_different_pid=$!
IFS= read -r _ < "$LOCK_FIFO"
IFS= read -r _ < "$LOCK_FIFO"
IFS= read -r _ < "$BUILD_FIFO"
docker_test_materialize_image second-different "$SECOND_DIFFERENT" -t "$SECOND_DIFFERENT" . &
second_different_pid=$!
IFS= read -r _ < "$LOCK_FIFO"
IFS= read -r _ < "$LOCK_FIFO"
[ "$(wc -l < "$STATE/builds")" -eq 2 ] || fail "shared exporter admitted concurrent imports"
printf 'release\n' > "$STATE/release-different"
wait "$first_different_pid"
IFS= read -r _ < "$BUILD_FIFO"
printf 'release\n' > "$STATE/release-different"
wait "$second_different_pid"
unset DOCKER_TEST_FAKE_RELEASE_FIFO DOCKER_TEST_FAKE_LOCK_FIFO DOCKER_TEST_FAKE_BUILD_FIFO

HOLD_FIFO="$STATE/hold-builder"
BUILDER_HELD_FIFO="$STATE/builder-held"
WARM_DONE_FIFO="$STATE/warm-done"
mkfifo "$HOLD_FIFO" "$BUILDER_HELD_FIFO" "$WARM_DONE_FIFO"
docker_test_with_builder_lock bash -c \
  'printf "held\n" > "$1"; IFS= read -r _ < "$2"' _ \
  "$BUILDER_HELD_FIFO" "$HOLD_FIFO" &
holder_pid=$!
cat "$BUILDER_HELD_FIFO" >/dev/null
if docker_test_try_with_builder_lock true; then
  fail "nonblocking builder lock ignored active holder"
else
  busy_status=$?
fi
[ "$busy_status" -eq 75 ] || fail "builder contention did not return reserved busy status"
(docker_test_materialize_image "$FINGERPRINT" "$IMAGE" -t "$IMAGE" .; printf 'done\n' > "$WARM_DONE_FIFO") &
warm_pid=$!
cat "$WARM_DONE_FIFO" >/dev/null
printf 'release\n' > "$HOLD_FIFO"
wait "$warm_pid" "$holder_pid"
if docker_test_try_with_builder_lock bash -c 'exit 75'; then
  fail "command failure under try-lock was reported as success"
else
  command_status=$?
fi
[ "$command_status" -eq 74 ] || fail "command failure collided with reserved busy status"

FAILED_IMAGE="better-agent-backend-tests:deps-failed"
if DOCKER_TEST_FAKE_FAIL_BUILD=1 \
  docker_test_materialize_image failed "$FAILED_IMAGE" -t "$FAILED_IMAGE" .; then
  fail "leader build failure was reported as success"
fi
docker_test_materialize_image failed "$FAILED_IMAGE" -t "$FAILED_IMAGE" .
[ "$(wc -l < "$STATE/builds")" -eq 5 ] || fail "failed leader did not release ownership for retry"

STALLED_IMAGE="better-agent-backend-tests:deps-stalled"
STALL_STDERR="$STATE/stall-stderr"
STALL_START="$(date +%s)"
if DOCKER_TEST_FAKE_STALL_BUILD=1 BETTER_AGENT_DOCKER_BUILD_STALL_SECONDS=1 \
  docker_test_materialize_image stalled "$STALLED_IMAGE" -t "$STALLED_IMAGE" . \
  2> "$STALL_STDERR"; then
  fail "stalled build was reported as success"
fi
STALL_ELAPSED=$(( $(date +%s) - STALL_START ))
[ "$STALL_ELAPSED" -lt 30 ] || fail "stall watchdog did not bound a hung build (took ${STALL_ELAPSED}s)"
[ "$(wc -l < "$STATE/builds")" -eq 7 ] || fail "stalled build was not retried exactly once"
grep -q 'retrying (attempt 2/2)' "$STALL_STDERR" || fail "stall retry was not announced"
grep -q 'build stalled twice' "$STALL_STDERR" || fail "double stall did not fail loudly"
! pgrep -f 'sleep 6317' >/dev/null 2>&1 || fail "stall watchdog leaked a hung build process"

docker_test_materialize_image healthy-after-stall \
  "better-agent-backend-tests:deps-healthy-after-stall" \
  -t "better-agent-backend-tests:deps-healthy-after-stall" .
[ "$(wc -l < "$STATE/builds")" -eq 8 ] || fail "healthy build after stall did not run"

docker_test_prepare_lock_root
rm -f "$DOCKER_TEST_LEASE_DIR/$DOCKER_TEST_RUN_ID"

echo "PASS: Docker test images are content-validated and shared exporter materialization is single-flight"
