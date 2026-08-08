#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/docker-test-lifecycle.sh
source "$HERE/lib/docker-test-lifecycle.sh"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/better-agent-docker-test.XXXXXX")"
TEST_LOCK_ROOT="/tmp/better-agent-docker-build-locks-test-$$"
trap 'rm -rf "$TMP_ROOT" "$TEST_LOCK_ROOT"' EXIT HUP INT TERM
LOG="$TMP_ROOT/docker.log"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_logged() {
  grep -F -- "$1" "$LOG" >/dev/null || fail "missing docker call: $1"
}

assert_not_logged() {
  if grep -F -- "$1" "$LOG" >/dev/null; then
    fail "unexpected docker call: $1"
  fi
}

docker() {
  printf '%s\n' "$*" >> "$LOG"
  case "$1 $2" in
    "context inspect") printf '%s\n' unix:///tmp/fake-docker.sock ;;
    "version --format") printf '%s\n' linux/amd64 ;;
    "ps --filter") printf '%s\n' orphan live foreign ;;
    "ps -q") printf '%s\n' running-builder ;;
    "inspect --format")
      case "$4" in
        orphan) printf '%s\n' 'host-a|999999|dead-start' ;;
        live) printf '%s\n' "host-a|$$|$DOCKER_TEST_OWNER_START" ;;
        foreign) printf '%s\n' 'host-b|999999|dead-start' ;;
        buildx_buildkit_*)
          if [ -n "${DOCKER_TEST_FAKE_BUILDER_OFF_NETWORK:-}" ]; then
            printf '%s \n' legacy-net
          else
            printf '%s \n' "$DOCKER_TEST_REGISTRY_NETWORK"
          fi
          ;;
      esac
      ;;
    "image inspect") printf '%s\n' old ;;
    "image ls")
      printf '%s\n' '2026-08-03T00:00:00Z|new' '2026-08-02T00:00:00Z|kept' '2026-08-01T00:00:00Z|old'
      ;;
    "ps -aq")
      [ "${4:-}" = "ancestor=kept" ] && printf '%s\n' using-kept
      ;;
    "buildx inspect") return 0 ;;
    "volume inspect")
      [ "${DOCKER_TEST_FAKE_VOLUME_MISSING:-}" != "1" ]
      ;;
    "run --rm")
      printf '%s\t/data\n' "${DOCKER_TEST_FAKE_VOLUME_BYTES:-0}"
      ;;
  esac
}

docker_test_process_start() {
  [ "$1" = "$$" ] && printf '%s\n' "$DOCKER_TEST_OWNER_START"
}

hostname() {
  printf '%s\n' host-a
}

id() {
  case "${1:-}" in
    # -u's format is load-bearing for the symlink-lock-root test below,
    # which independently derives TEST_LOCK_ROOT from the same "test-$$"
    # shape docker_test_prepare_lock_root computes via `id -u`.
    -u) printf 'test-%s\n' "$$" ;;
    -g) printf 'test-gid-%s\n' "$$" ;;
  esac
}

FAKE_UNAME_S="Darwin"
uname() {
  [ "${1:-}" = -s ] && printf '%s\n' "$FAKE_UNAME_S"
}

docker_test_with_builder_lock() {
  "$@"
}

: > "$LOG"
DOCKER_TEST_OWNER_START="live-start"
docker_test_lifecycle_init backend
trap - EXIT
docker_test_reap_orphans
assert_logged "rm -f orphan"
assert_not_logged "rm -f live"
assert_not_logged "rm -f foreign"

: > "$LOG"
BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT=1
docker_test_prune_images
assert_logged "image rm old"
assert_not_logged "image rm new"
assert_not_logged "image rm kept"

: > "$LOG"
docker_test_prepare_lock_root
printf 'leased-tag|host-a|%s|%s\n' "$$" "$DOCKER_TEST_OWNER_START" \
  > "$DOCKER_TEST_LEASE_DIR/live-lease"
docker_test_prune_images_for_kind deps 0
assert_not_logged "image rm old"
rm -f "$DOCKER_TEST_LEASE_DIR/live-lease"

: > "$LOG"
docker_test_prune_build_cache_unlocked
assert_logged "buildx prune --builder better-agent-tests --force --max-used-space 10GB"
assert_not_logged "volume"
assert_not_logged "system prune"

[ "$(docker_test_size_to_bytes 10GB)" -eq 10737418240 ] || fail "size_to_bytes: 10GB parsed incorrectly"
[ "$(docker_test_size_to_bytes 512MB)" -eq 536870912 ] || fail "size_to_bytes: 512MB parsed incorrectly"
[ "$(docker_test_size_to_bytes 100)" -eq 100 ] || fail "size_to_bytes: bare byte count parsed incorrectly"

# Registry storage is a disposable cache: below the cap it is left alone;
# once it exceeds the cap, container+volume are deleted so the next
# materialize recreates them fresh (no in-place GC).
: > "$LOG"
DOCKER_TEST_FAKE_VOLUME_BYTES=$((5 * 1024 * 1024 * 1024))
docker_test_ensure_registry_within_cap
assert_not_logged "rm -f $DOCKER_TEST_REGISTRY"
assert_not_logged "volume rm"

: > "$LOG"
DOCKER_TEST_FAKE_VOLUME_BYTES=$((20 * 1024 * 1024 * 1024))
docker_test_ensure_registry_within_cap
assert_logged "rm -f $DOCKER_TEST_REGISTRY"
assert_logged "volume rm $DOCKER_TEST_REGISTRY_VOLUME"
unset DOCKER_TEST_FAKE_VOLUME_BYTES

: > "$LOG"
DOCKER_TEST_FAKE_VOLUME_MISSING=1
docker_test_registry_volume_bytes_result="$(docker_test_registry_volume_bytes)"
[ "$docker_test_registry_volume_bytes_result" -eq 0 ] || fail "missing registry volume did not report 0 bytes"
unset DOCKER_TEST_FAKE_VOLUME_MISSING

# Builder network attachment is migrated only when missing, under the
# builder lock, so concurrent sessions never see a half-configured builder.
: > "$LOG"
docker_test_ensure_builder_on_network
assert_not_logged "buildx rm"
assert_not_logged "buildx create --name"

: > "$LOG"
DOCKER_TEST_FAKE_BUILDER_OFF_NETWORK=1
docker_test_ensure_builder_on_network
assert_logged "buildx rm better-agent-tests"
assert_logged "buildx create --name better-agent-tests --driver docker-container --driver-opt network=$DOCKER_TEST_REGISTRY_NETWORK"
unset DOCKER_TEST_FAKE_BUILDER_OFF_NETWORK

: > "$LOG"
docker_test_run --rm example -k smoke
assert_logged "run --name better-agent-test-backend-"
assert_logged "--label com.better-agent.test.run="
assert_not_logged "volume rm"

# Resource caps: unset env preserves the long-standing --cpus=2 default and
# emits neither --memory nor --cpu-shares (both previously absent).
unset BETTER_AGENT_TEST_CPUS BETTER_AGENT_TEST_MEMORY BETTER_AGENT_TEST_CPU_SHARES
default_cap_args="$(docker_test_resource_cap_args)"
[ "$default_cap_args" = "--cpus=2" ] \
  || fail "default resource cap args changed local behavior: $default_cap_args"

# Setting the knobs must reach the actual docker invocation, memory paired
# with a matching --memory-swap (no swap thrash), unset shares still absent.
RUN_ARGS_UNDER_TEST=()
while IFS= read -r cap_arg; do
  RUN_ARGS_UNDER_TEST+=("$cap_arg")
done < <(BETTER_AGENT_TEST_CPUS=4 BETTER_AGENT_TEST_MEMORY=2g docker_test_resource_cap_args)
[ "${RUN_ARGS_UNDER_TEST[*]}" = "--cpus=4 --memory=2g --memory-swap=2g" ] \
  || fail "set BETTER_AGENT_TEST_CPUS/_MEMORY did not produce expected docker flags: ${RUN_ARGS_UNDER_TEST[*]}"

: > "$LOG"
docker_test_run "${RUN_ARGS_UNDER_TEST[@]}" example -k smoke
assert_logged "run --name better-agent-test-backend-"
assert_logged "--cpus=4 --memory=2g --memory-swap=2g example -k smoke"

# xdist pytest args (docker_test_xdist_pytest_args): the knob passed as "" (the
# local-dev default, whether from an unset --parallel or an unset
# BETTER_AGENT_TEST_XDIST) must emit nothing so the pytest invocation stays
# single-process; a set value must reach the actual docker invocation as
# `-n <value> --dist loadfile` (loadfile is mandatory, never loadscope/load —
# see the function's own header).
[ -z "$(docker_test_xdist_pytest_args "")" ] \
  || fail "docker_test_xdist_pytest_args emitted flags for an unset worker count"

XDIST_ARGS_UNDER_TEST=()
while IFS= read -r xdist_arg; do
  XDIST_ARGS_UNDER_TEST+=("$xdist_arg")
done < <(docker_test_xdist_pytest_args 4)
[ "${XDIST_ARGS_UNDER_TEST[*]}" = "-n 4 --dist loadfile" ] \
  || fail "docker_test_xdist_pytest_args did not produce expected pytest flags: ${XDIST_ARGS_UNDER_TEST[*]}"

: > "$LOG"
docker_test_run "${XDIST_ARGS_UNDER_TEST[@]}" example -k smoke
assert_logged "run --name better-agent-test-backend-"
assert_logged "-n 4 --dist loadfile example -k smoke"

: > "$LOG"
docker_test_run example -k smoke
assert_not_logged "--dist loadfile"

grep -F 'docker_test_xdist_pytest_args' "$HERE/run-backend-tests.sh" >/dev/null \
  || fail "run-backend-tests.sh does not forward docker_test_xdist_pytest_args into PYTEST_ARGS"

# CPU shares is an independent, still-optional knob (relative weight, not a
# hard ceiling) — absent unless explicitly set.
shares_cap_args="$(BETTER_AGENT_TEST_CPU_SHARES=512 docker_test_resource_cap_args)"
[ "$shares_cap_args" = "$(printf '%s\n%s' '--cpus=2' '--cpu-shares=512')" ] \
  || fail "set BETTER_AGENT_TEST_CPU_SHARES did not produce expected docker flag: $shares_cap_args"

# BETTER_AGENT_TEST_CHOWN forwarding (docker_test_chown_env_args): must
# reach the docker invocation on native Linux Docker hosts, so
# entrypoint-test.sh can hand bind-mounted /repo ownership back to the
# invoking user, and must be a complete no-op everywhere else (macOS/Docker
# Desktop already remaps bind-mount ownership).
FAKE_UNAME_S="Darwin"
darwin_chown_args="$(docker_test_chown_env_args)"
[ -z "$darwin_chown_args" ] \
  || fail "docker_test_chown_env_args emitted flags on Darwin: $darwin_chown_args"

FAKE_UNAME_S="Linux"
linux_chown_args="$(docker_test_chown_env_args)"
[ "$linux_chown_args" = "$(printf -- '-e\nBETTER_AGENT_TEST_CHOWN=test-%s:test-gid-%s' "$$" "$$")" ] \
  || fail "docker_test_chown_env_args did not forward uid:gid on Linux: $linux_chown_args"

CHOWN_ARGS_UNDER_TEST=()
while IFS= read -r chown_arg; do
  CHOWN_ARGS_UNDER_TEST+=("$chown_arg")
done < <(docker_test_chown_env_args)
: > "$LOG"
docker_test_run "${CHOWN_ARGS_UNDER_TEST[@]}" example -k smoke
assert_logged "-e BETTER_AGENT_TEST_CHOWN=test-$$:test-gid-$$ example -k smoke"

FAKE_UNAME_S="Darwin"
CHOWN_ARGS_UNDER_TEST_DARWIN=()
while IFS= read -r chown_arg; do
  CHOWN_ARGS_UNDER_TEST_DARWIN+=("$chown_arg")
done < <(docker_test_chown_env_args)
[ "${#CHOWN_ARGS_UNDER_TEST_DARWIN[@]}" -eq 0 ] \
  || fail "docker_test_chown_env_args produced flags on Darwin: ${CHOWN_ARGS_UNDER_TEST_DARWIN[*]}"
: > "$LOG"
docker_test_run ${CHOWN_ARGS_UNDER_TEST_DARWIN[@]+"${CHOWN_ARGS_UNDER_TEST_DARWIN[@]}"} example -k smoke
assert_not_logged "BETTER_AGENT_TEST_CHOWN"

grep -F 'docker_test_chown_env_args' "$HERE/run-backend-tests.sh" >/dev/null \
  || fail "run-backend-tests.sh does not forward docker_test_chown_env_args into RUN_ARGS"

for runner in "$HERE/run-backend-tests.sh" "$HERE/run-fullstack-tests.sh"; do
  grep -F 'source "$HERE/lib/docker-test-lifecycle.sh"' "$runner" >/dev/null \
    || fail "$runner does not source the shared lifecycle"
  grep -F 'docker_test_materialize_image ' "$runner" >/dev/null \
    || fail "$runner bypasses content-addressed image materialization"
  grep -F 'docker_test_run ' "$runner" >/dev/null \
    || fail "$runner bypasses the shared run owner"
  if grep -E '^[[:space:]]*docker (build|run) ' "$runner" >/dev/null; then
    fail "$runner retains a direct Docker build/run path"
  fi
done

FINGERPRINT_ROOT="$TMP_ROOT/fingerprint"
mkdir -p "$FINGERPRINT_ROOT"
printf 'one\n' > "$FINGERPRINT_ROOT/input.txt"
first_fingerprint="$(docker_test_fingerprint "$FINGERPRINT_ROOT" "$FINGERPRINT_ROOT/input.txt")"
touch "$FINGERPRINT_ROOT/input.txt"
mtime_fingerprint="$(docker_test_fingerprint "$FINGERPRINT_ROOT" "$FINGERPRINT_ROOT/input.txt")"
[ "$first_fingerprint" = "$mtime_fingerprint" ] || fail "mtime changed dependency fingerprint"
printf 'two\n' > "$FINGERPRINT_ROOT/input.txt"
content_fingerprint="$(docker_test_fingerprint "$FINGERPRINT_ROOT" "$FINGERPRINT_ROOT/input.txt")"
[ "$first_fingerprint" != "$content_fingerprint" ] || fail "content change did not invalidate dependency fingerprint"

ln -s target "$FINGERPRINT_ROOT/symlink"
plain_symlink_fingerprint="$(docker_test_fingerprint "$FINGERPRINT_ROOT" "$FINGERPRINT_ROOT/symlink")"
rm "$FINGERPRINT_ROOT/symlink"
ln -s $'target\n' "$FINGERPRINT_ROOT/symlink"
newline_symlink_fingerprint="$(docker_test_fingerprint "$FINGERPRINT_ROOT" "$FINGERPRINT_ROOT/symlink")"
[ "$plain_symlink_fingerprint" != "$newline_symlink_fingerprint" ] \
  || fail "trailing newline in symlink target was lost from fingerprint"

printf 'newline\n' > "$FINGERPRINT_ROOT/line
break.txt"
newline_fingerprint="$(docker_test_fingerprint "$FINGERPRINT_ROOT" "$FINGERPRINT_ROOT")"
[ -n "$newline_fingerprint" ] || fail "newline-containing path broke fingerprint enumeration"

SOURCE_ROOT="$TMP_ROOT/source"
SNAPSHOT_ROOT="$TMP_ROOT/snapshot"
mkdir -p "$SOURCE_ROOT/deps"
printf 'before\n' > "$SOURCE_ROOT/deps/input.txt"
docker_test_snapshot_context "$SOURCE_ROOT" "$SNAPSHOT_ROOT" "$SOURCE_ROOT/deps"
printf 'after\n' > "$SOURCE_ROOT/deps/input.txt"
[ "$(cat "$SNAPSHOT_ROOT/deps/input.txt")" = before ] || fail "dependency snapshot followed later source mutation"

GIT_ROOT="$TMP_ROOT/git"
REF_ROOT="$TMP_ROOT/ref"
git init -q "$GIT_ROOT"
git -C "$GIT_ROOT" config user.email test@example.invalid
git -C "$GIT_ROOT" config user.name test
printf 'selected\n' > "$GIT_ROOT/value.txt"
git -C "$GIT_ROOT" add value.txt
git -C "$GIT_ROOT" commit -qm selected
SELECTED_COMMIT="$(git -C "$GIT_ROOT" rev-parse HEAD)"
printf 'later\n' > "$GIT_ROOT/value.txt"
git -C "$GIT_ROOT" commit -qam later
docker_test_snapshot_git_ref "$GIT_ROOT" "$SELECTED_COMMIT" "$REF_ROOT"
[ "$(cat "$REF_ROOT/value.txt")" = selected ] || fail "ref snapshot did not use selected commit"

DOCKER_TEST_RUN_DIR="$TMP_ROOT/cleanup-run"
DOCKER_TEST_IMAGE_LEASE="$TMP_ROOT/cleanup-lease"
mkdir -p "$DOCKER_TEST_RUN_DIR"
: > "$DOCKER_TEST_IMAGE_LEASE"
docker_test_remove_current_container
[ ! -e "$DOCKER_TEST_RUN_DIR" ] || fail "run context was not cleaned"
[ ! -e "$TMP_ROOT/cleanup-lease" ] || fail "image lease was not cleaned"

rm -rf "$TEST_LOCK_ROOT"
UNSAFE_TARGET="$TMP_ROOT/unsafe-target"
mkdir "$UNSAFE_TARGET"
chmod 755 "$UNSAFE_TARGET"
ln -s "$UNSAFE_TARGET" "$TEST_LOCK_ROOT"
if docker_test_prepare_lock_root; then
  fail "symlink lock root was accepted"
fi
[ "$(stat -f '%Lp' "$UNSAFE_TARGET" 2>/dev/null || stat -c '%a' "$UNSAFE_TARGET")" = 755 ] \
  || fail "symlink target permissions were mutated"
rm -f "$TEST_LOCK_ROOT"

docker_test_with_state_lock() { return 19; }
docker_test_prune_build_cache_unlocked() { return 0; }
if docker_test_cleanup_locked; then
  fail "image-prune failure was hidden by later cache cleanup"
else
  cleanup_failure_status=$?
fi
[ "$cleanup_failure_status" -eq 19 ] || fail "cleanup failure status was not preserved"

rm -rf "$TMP_ROOT"
echo "PASS: Docker test lifecycle preserves live/foreign resources and bounds owned cache/images"
