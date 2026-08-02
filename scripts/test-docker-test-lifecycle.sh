#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/docker-test-lifecycle.sh
source "$HERE/lib/docker-test-lifecycle.sh"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/better-agent-docker-test.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT HUP INT TERM
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
    "ps --filter") printf '%s\n' orphan live foreign ;;
    "inspect --format")
      case "$4" in
        orphan) printf '%s\n' 'host-a|999999|dead-start' ;;
        live) printf '%s\n' "host-a|$$|$DOCKER_TEST_OWNER_START" ;;
        foreign) printf '%s\n' 'host-b|999999|dead-start' ;;
      esac
      ;;
    "image ls")
      printf '%s\n' '2026-08-03T00:00:00Z|new' '2026-08-02T00:00:00Z|kept' '2026-08-01T00:00:00Z|old'
      ;;
    "ps -aq")
      [ "${4:-}" = "ancestor=kept" ] && printf '%s\n' using-kept
      ;;
    "buildx inspect") return 0 ;;
  esac
}

docker_test_process_start() {
  [ "$1" = "$$" ] && printf '%s\n' "$DOCKER_TEST_OWNER_START"
}

hostname() {
  printf '%s\n' host-a
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
docker_test_prune_build_cache
assert_logged "buildx prune --builder better-agent-tests --force --max-used-space 10GB"
assert_not_logged "volume"
assert_not_logged "system prune"

: > "$LOG"
docker_test_build --target deps -t example .
assert_logged "buildx build --builder better-agent-tests --load"
assert_logged "--label com.better-agent.test.owner=true"
assert_logged "--label com.better-agent.test.family=backend"

: > "$LOG"
docker_test_run --rm example -k smoke
assert_logged "run --name better-agent-test-backend-"
assert_logged "--label com.better-agent.test.run="
assert_not_logged "volume rm"

for runner in "$HERE/run-backend-tests.sh" "$HERE/run-fullstack-tests.sh"; do
  grep -F 'source "$HERE/lib/docker-test-lifecycle.sh"' "$runner" >/dev/null \
    || fail "$runner does not source the shared lifecycle"
  grep -F 'docker_test_build ' "$runner" >/dev/null \
    || fail "$runner bypasses the shared build owner"
  grep -F 'docker_test_run ' "$runner" >/dev/null \
    || fail "$runner bypasses the shared run owner"
  if grep -E '^[[:space:]]*docker (build|run) ' "$runner" >/dev/null; then
    fail "$runner retains a direct Docker build/run path"
  fi
done

rm -rf "$TMP_ROOT"
echo "PASS: Docker test lifecycle preserves live/foreign resources and bounds owned cache/images"
