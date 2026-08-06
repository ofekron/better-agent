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

# Fake docker models the push -> pull -> tag handoff: a build+push writes the
# image content keyed by its registry-relative path (registry-image-<key>,
# simulating the registry's store); a pull reads that same key regardless of
# which host:port addressed it (container-name push ref vs localhost pull
# ref) and materializes it into the engine's local store (image-<tag>,
# read by `docker image inspect`).
cat > "$FAKE_BIN/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
safe_tag() { printf '%s' "$1" | tr '/:' '__'; }
registry_path() { printf '%s' "$1" | sed 's#^[^/]*/##'; }

case "${1:-}" in
  pull)
    ref="${2:-}"
    [ -z "${DOCKER_TEST_FAKE_PULL_FAIL:-}" ] || exit 1
    key="$DOCKER_TEST_FAKE_STATE/registry-image-$(safe_tag "$(registry_path "$ref")")"
    [ -f "$key" ] || exit 1
    content="$(cat "$key")"
    if [ -n "${DOCKER_TEST_FAKE_PULL_CORRUPT:-}" ]; then
      content="corrupted-$content"
    fi
    printf '%s\n' "$content" > "$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "$ref")"
    exit 0
    ;;
  tag)
    src="${2:-}"
    dst="${3:-}"
    cp "$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "$src")" "$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "$dst")"
    exit 0
    ;;
  rmi)
    rm -f "$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "${2:-}")"
    exit 0
    ;;
esac

case "${1:-} ${2:-}" in
  "context inspect") printf '%s\n' unix:///tmp/fake-docker.sock ;;
  "version --format") printf '%s\n' linux/amd64 ;;
  "buildx inspect") exit 0 ;;
  "inspect --format")
    # Builder-network-attachment probe: report the builder as already on the
    # expected registry network so these scenarios don't exercise migration.
    printf '%s\n' "${DOCKER_TEST_FAKE_BUILDER_NETWORK:-better-agent-test-net}"
    ;;
  "ps -q")
    # Registry-running probe: report already running unless a scenario
    # explicitly simulates the registry being down.
    [ -n "${DOCKER_TEST_FAKE_REGISTRY_DOWN:-}" ] || printf 'fake-registry-container-id\n'
    ;;
  "image inspect")
    tag="${@: -1}"
    file="$DOCKER_TEST_FAKE_STATE/image-$(safe_tag "$tag")"
    [ -f "$file" ] || exit 1
    cat "$file"
    ;;
  "buildx build")
    output_spec=""
    fingerprint=""
    previous=""
    for arg in "$@"; do
      if [ "$previous" = "--output" ]; then output_spec="$arg"; fi
      case "$arg" in
        com.better-agent.test.fingerprint=*) fingerprint="${arg#*=}" ;;
      esac
      previous="$arg"
    done
    push_ref="${output_spec#*name=}"
    push_ref="${push_ref%%,*}"
    printf 'build %s %s\n' "$push_ref" "$fingerprint" >> "$DOCKER_TEST_FAKE_STATE/builds"
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
    printf '%s\n' "$fingerprint" > "$DOCKER_TEST_FAKE_STATE/registry-image-$(safe_tag "$(registry_path "$push_ref")")"
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$FAKE_BIN/docker"

cat > "$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
[ -z "${DOCKER_TEST_FAKE_REGISTRY_DOWN:-}" ]
EOF
chmod +x "$FAKE_BIN/curl"

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

# The shell's own `< fifo` redirection (blocking open, waiting for a writer)
# can be interrupted by SIGCHLD from the extra concurrent flock/lockf
# subprocesses these scenarios now spawn; retry the open+read on EINTR
# rather than treating a spurious signal as end-of-data.
drain_fifo() {
  until IFS= read -r _ < "$1"; do :; done
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
drain_fifo "$LOCK_FIFO"   # state-check lock
drain_fifo "$LOCK_FIFO"   # ensure_infra builder lock (registry + builder-network)
drain_fifo "$LOCK_FIFO"   # materialize builder lock
drain_fifo "$BUILD_FIFO"
docker_test_materialize_image "$FINGERPRINT" "$IMAGE" -t "$IMAGE" . &
second_pid=$!
drain_fifo "$LOCK_FIFO"   # follower state-check
drain_fifo "$LOCK_FIFO"   # follower ensure_infra builder-lock attempt (blocks on leader's held materialize lock)
[ "$(wc -l < "$STATE/builds")" -eq 1 ] || fail "same-key follower started a duplicate export"
printf 'release\n' > "$FIFO"
# Once unblocked, the follower's ensure_infra completes and it makes its own
# materialize builder-lock attempt (a distinct flock call from ensure_infra's);
# that attempt still signals the lock-fifo even though the fingerprint now
# matches and it short-circuits without rebuilding.
drain_fifo "$LOCK_FIFO"
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
drain_fifo "$LOCK_FIFO"
drain_fifo "$LOCK_FIFO"
drain_fifo "$LOCK_FIFO"
drain_fifo "$BUILD_FIFO"
docker_test_materialize_image second-different "$SECOND_DIFFERENT" -t "$SECOND_DIFFERENT" . &
second_different_pid=$!
drain_fifo "$LOCK_FIFO"   # state-check
drain_fifo "$LOCK_FIFO"   # ensure_infra builder-lock attempt (blocks on first_different's held materialize lock)
[ "$(wc -l < "$STATE/builds")" -eq 2 ] || fail "shared exporter admitted concurrent imports"
printf 'release\n' > "$STATE/release-different"
wait "$first_different_pid"
# Once unblocked, second_different's ensure_infra completes and it makes its
# own materialize builder-lock attempt (a distinct flock call), then genuinely
# rebuilds (different fingerprint => no cache-hit short-circuit).
drain_fifo "$LOCK_FIFO"
drain_fifo "$BUILD_FIFO"
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

# No silent fallback to --load: a registry that never becomes reachable must
# fail loudly, without ever attempting a build.
UNREACHABLE_IMAGE="better-agent-backend-tests:deps-unreachable"
UNREACHABLE_STDERR="$STATE/unreachable-stderr"
BUILDS_BEFORE_UNREACHABLE="$(wc -l < "$STATE/builds")"
if DOCKER_TEST_FAKE_REGISTRY_DOWN=1 BETTER_AGENT_DOCKER_REGISTRY_READY_ATTEMPTS=2 \
  docker_test_materialize_image unreachable "$UNREACHABLE_IMAGE" -t "$UNREACHABLE_IMAGE" . \
  2> "$UNREACHABLE_STDERR"; then
  fail "materialize succeeded despite an unreachable registry"
fi
[ "$(wc -l < "$STATE/builds")" -eq "$BUILDS_BEFORE_UNREACHABLE" ] \
  || fail "unreachable registry still attempted a build instead of failing loudly up front"
grep -q 'test registry did not become ready' "$UNREACHABLE_STDERR" \
  || fail "registry-unreachable did not fail loudly with a recovery hint"
grep -q 'docker rm -f' "$UNREACHABLE_STDERR" \
  || fail "registry-unreachable failure omitted a nuke-and-recreate recovery hint"

# A pull that returns content not matching the fingerprint that was pushed
# must be rejected, not silently accepted as the canonical image.
CORRUPT_IMAGE="better-agent-backend-tests:deps-corrupt"
CORRUPT_STDERR="$STATE/corrupt-stderr"
if DOCKER_TEST_FAKE_PULL_CORRUPT=1 \
  docker_test_materialize_image corrupt "$CORRUPT_IMAGE" -t "$CORRUPT_IMAGE" . \
  2> "$CORRUPT_STDERR"; then
  fail "materialize succeeded despite a fingerprint-mismatched pull"
fi
grep -q 'pulled image failed fingerprint validation' "$CORRUPT_STDERR" \
  || fail "corrupt pull was not fingerprint-validated"

# A pull that fails right after a successful push is a genuine error (wrong
# ref, registry down, network) — not a propagation-delay timing window.
# Single attempt, fail loud immediately, no sleep-retry.
PULL_FAIL_IMAGE="better-agent-backend-tests:deps-pull-fail"
PULL_FAIL_STDERR="$STATE/pull-fail-stderr"
BUILDS_BEFORE_PULL_FAIL="$(wc -l < "$STATE/builds")"
PULL_FAIL_START="$(date +%s)"
if DOCKER_TEST_FAKE_PULL_FAIL=1 \
  docker_test_materialize_image pull-fail "$PULL_FAIL_IMAGE" -t "$PULL_FAIL_IMAGE" . \
  2> "$PULL_FAIL_STDERR"; then
  fail "materialize succeeded despite a failed pull"
fi
PULL_FAIL_ELAPSED=$(( $(date +%s) - PULL_FAIL_START ))
[ "$PULL_FAIL_ELAPSED" -lt 5 ] || fail "pull failure was not immediate (took ${PULL_FAIL_ELAPSED}s, suggests a retry-sleep crept back in)"
[ "$(wc -l < "$STATE/builds")" -eq $((BUILDS_BEFORE_PULL_FAIL + 1)) ] \
  || fail "pull-fail scenario did not push exactly once before failing"
grep -q 'engine pull failed' "$PULL_FAIL_STDERR" || fail "pull failure did not fail loudly"
grep -q 'docker rm -f' "$PULL_FAIL_STDERR" || fail "pull failure omitted a recovery hint"

docker_test_prepare_lock_root
rm -f "$DOCKER_TEST_LEASE_DIR/$DOCKER_TEST_RUN_ID"

echo "PASS: Docker test images are content-validated and shared exporter materialization is single-flight"
