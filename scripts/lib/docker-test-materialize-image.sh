#!/usr/bin/env bash
set -euo pipefail

MODE="$1"
BUILDER="$2"
FAMILY="$3"
KIND="$4"
FINGERPRINT="$5"
IMAGE_TAG="$6"
LEASE_FILE="$7"
LEASE_VALUE="$8"
shift 8

case "$MODE" in check|materialize) ;; *) exit 64 ;; esac

LABEL_PREFIX="com.better-agent.test"
current_fingerprint="$(docker image inspect \
  --format "{{index .Config.Labels \"${LABEL_PREFIX}.fingerprint\"}}" \
  "$IMAGE_TAG" 2>/dev/null || true)"
if [ "$current_fingerprint" = "$FINGERPRINT" ]; then
  printf '%s\n' "$LEASE_VALUE" > "$LEASE_FILE.tmp.$$"
  mv "$LEASE_FILE.tmp.$$" "$LEASE_FILE"
  echo "docker-test-lifecycle: reusing $IMAGE_TAG"
  exit 0
fi
if [ "$MODE" = check ]; then
  exit 75
fi

# Stall watchdog: a wedged buildkit solve or --load tarball transfer can
# otherwise hold the shared builder lock indefinitely and starve every
# concurrent session (observed 2026-08-06: a deps --load hung 4h). A build
# that emits no progress output for this many seconds is killed and retried
# once; a second stall fails the run loudly.
BUILD_STALL_SECONDS="${BETTER_AGENT_DOCKER_BUILD_STALL_SECONDS:-600}"
case "$BUILD_STALL_SECONDS" in
  *[!0-9]*|'')
    echo "docker-test-lifecycle: BETTER_AGENT_DOCKER_BUILD_STALL_SECONDS must be a positive integer" >&2
    exit 64
    ;;
esac

buildx_build_watched() {
  local fifo="$1" pid line status=0 error_marker=""
  shift
  docker buildx build --builder "$BUILDER" --load --progress=plain \
    --label "${LABEL_PREFIX}.owner=true" \
    --label "${LABEL_PREFIX}.family=${FAMILY}" \
    --label "${LABEL_PREFIX}.kind=${KIND}" \
    --label "${LABEL_PREFIX}.fingerprint=${FINGERPRINT}" \
    "$@" >"$fifo" 2>&1 &
  pid=$!
  exec 3<"$fifo"
  while :; do
    if IFS= read -r -t "$BUILD_STALL_SECONDS" line <&3; then
      printf '%s\n' "$line"
      # buildx's own top-level failure summary, printed exactly once right
      # before the CLI exits nonzero. Remembered so it can be cross-checked
      # against the process's actual exit status below: a cancelled/dropped
      # `--load` export (daemon-side "Canceled"/"DeadlineExceeded", observed
      # on this host's buildkit container during export) has been seen to
      # leave the CLIENT reporting exit 0 despite printing this line, which
      # would otherwise make a failed build look like a successful one.
      case "$line" in
        "ERROR: failed to build:"*) error_marker="$line" ;;
      esac
      continue
    fi
    # read failed: timeout or EOF. bash 3.2 (stock macOS) returns 1 for BOTH,
    # so the exit status cannot distinguish them — but a RUNNING writer holds
    # the fifo's write end open, making EOF impossible; a failed read with the
    # build still running is therefore a stall by construction. The check must
    # be process STATE, not kill -0: the unreaped child of a just-finished
    # build is a zombie, and kill -0 succeeds on zombies.
    build_state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
    case "$build_state" in ''|Z*) break ;; esac
    exec 3<&-
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    if [ -n "$error_marker" ]; then
      echo "docker-test-lifecycle: build already reported a failure before stalling — not retrying:" >&2
      printf '%s\n' "$error_marker" >&2
      return 1
    fi
    return 76
  done
  exec 3<&-
  status=0
  wait "$pid" || status=$?
  if [ "$status" -eq 0 ] && [ -n "$error_marker" ]; then
    # The CLI claims success but its own output reported a build failure.
    # Trust the tool's diagnostic text over its exit code: this is exactly
    # the "red build masquerading as green" case a lying exit status would
    # otherwise let through the `[ "$build_status" -eq 0 ]` check below.
    echo "docker-test-lifecycle: buildx exited 0 but emitted a build failure line — treating as failed (exit-code/output desync, likely a cancelled --load export):" >&2
    printf '%s\n' "$error_marker" >&2
    status=1
  fi
  return "$status"
}

build_attempt=1
while :; do
  BUILD_FIFO_PATH="$(mktemp -u "${TMPDIR:-/tmp}/better-agent-build-watch.XXXXXX")"
  mkfifo "$BUILD_FIFO_PATH"
  build_status=0
  buildx_build_watched "$BUILD_FIFO_PATH" "$@" || build_status=$?
  rm -f "$BUILD_FIFO_PATH"
  if [ "$build_status" -eq 76 ]; then
    if [ "$build_attempt" -lt 2 ]; then
      echo "docker-test-lifecycle: build emitted no progress for ${BUILD_STALL_SECONDS}s; killed, retrying (attempt 2/2)" >&2
      build_attempt=2
      continue
    fi
    echo "docker-test-lifecycle: build stalled twice (no progress for ${BUILD_STALL_SECONDS}s each) — giving up. The '$BUILDER' builder may be wedged: inspect 'docker logs buildx_buildkit_${BUILDER}0', then 'docker buildx rm $BUILDER' (recreated on demand)." >&2
    exit 1
  fi
  [ "$build_status" -eq 0 ] || exit "$build_status"
  break
done

loaded_fingerprint="$(docker image inspect \
  --format "{{index .Config.Labels \"${LABEL_PREFIX}.fingerprint\"}}" \
  "$IMAGE_TAG" 2>/dev/null || true)"
if [ "$loaded_fingerprint" != "$FINGERPRINT" ]; then
  echo "docker-test-lifecycle: loaded image failed fingerprint validation: $IMAGE_TAG" >&2
  exit 1
fi
printf '%s\n' "$LEASE_VALUE" > "$LEASE_FILE.tmp.$$"
mv "$LEASE_FILE.tmp.$$" "$LEASE_FILE"
