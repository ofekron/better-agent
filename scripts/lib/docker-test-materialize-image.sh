#!/usr/bin/env bash
set -euo pipefail

MODE="$1"
BUILDER="$2"
REGISTRY_HOST="$3"
REGISTRY_PORT="$4"
REGISTRY_VOLUME="$5"
FAMILY="$6"
KIND="$7"
FINGERPRINT="$8"
IMAGE_TAG="$9"
LEASE_FILE="${10}"
LEASE_VALUE="${11}"
shift 11

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

registry_recovery_hint() {
  echo "Recovery: inspect 'docker logs $REGISTRY_HOST'; if wedged, nuke it with 'docker rm -f $REGISTRY_HOST && docker volume rm $REGISTRY_VOLUME' (recreated on demand)." >&2
}

# The caller composes build args for the old --load flow, including a bare
# (unqualified) -t/--tag pair. Push destination is controlled exclusively by
# --output below; forwarding that bare tag alongside an explicit --output
# would make buildx additionally try to push it to the default docker.io
# registry. Strip it rather than asking callers to change.
BUILD_ARGS=()
skip_next=0
for arg in "$@"; do
  if [ "$skip_next" -eq 1 ]; then skip_next=0; continue; fi
  case "$arg" in
    -t|--tag) skip_next=1; continue ;;
  esac
  BUILD_ARGS+=("$arg")
done

PUSH_REF="${REGISTRY_HOST}:5000/${IMAGE_TAG}"
PULL_REF="localhost:${REGISTRY_PORT}/${IMAGE_TAG}"

# Stall watchdog: a wedged buildkit solve or push transfer can otherwise
# hold the shared builder lock indefinitely and starve every concurrent
# session (observed 2026-08-06: a deps --load hung 4h, same class of risk
# applies to a wedged push). A build that emits no progress output for this
# many seconds is killed and retried once; a second stall fails the run
# loudly.
BUILD_STALL_SECONDS="${BETTER_AGENT_DOCKER_BUILD_STALL_SECONDS:-600}"
case "$BUILD_STALL_SECONDS" in
  *[!0-9]*|'')
    echo "docker-test-lifecycle: BETTER_AGENT_DOCKER_BUILD_STALL_SECONDS must be a positive integer" >&2
    exit 64
    ;;
esac

buildx_build_watched() {
  local fifo="$1" pid line status=0
  shift
  docker buildx build --builder "$BUILDER" --progress=plain \
    --label "${LABEL_PREFIX}.owner=true" \
    --label "${LABEL_PREFIX}.family=${FAMILY}" \
    --label "${LABEL_PREFIX}.kind=${KIND}" \
    --label "${LABEL_PREFIX}.fingerprint=${FINGERPRINT}" \
    --output "type=image,name=${PUSH_REF},push=true,registry.insecure=true" \
    "$@" >"$fifo" 2>&1 &
  pid=$!
  exec 3<"$fifo"
  while :; do
    if IFS= read -r -t "$BUILD_STALL_SECONDS" line <&3; then
      printf '%s\n' "$line"
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
    return 76
  done
  exec 3<&-
  status=0
  wait "$pid" || status=$?
  return "$status"
}

build_attempt=1
while :; do
  BUILD_FIFO_PATH="$(mktemp -u "${TMPDIR:-/tmp}/better-agent-build-watch.XXXXXX")"
  mkfifo "$BUILD_FIFO_PATH"
  build_status=0
  buildx_build_watched "$BUILD_FIFO_PATH" "${BUILD_ARGS[@]}" || build_status=$?
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
  if [ "$build_status" -ne 0 ]; then
    echo "docker-test-lifecycle: build+push to $PUSH_REF failed." >&2
    registry_recovery_hint
    exit "$build_status"
  fi
  break
done

# A successful push means the registry already serves the manifest — a
# failed pull right after is a genuine error (wrong ref, registry down,
# network), not a propagation-delay timing window. One attempt, fail loud;
# the build+push watchdog above already bounds hangs in this same script.
if ! docker pull "$PULL_REF" >/dev/null 2>&1; then
  echo "docker-test-lifecycle: engine pull failed: $PULL_REF." >&2
  registry_recovery_hint
  exit 1
fi

docker tag "$PULL_REF" "$IMAGE_TAG"
docker rmi "$PULL_REF" >/dev/null 2>&1 || true

loaded_fingerprint="$(docker image inspect \
  --format "{{index .Config.Labels \"${LABEL_PREFIX}.fingerprint\"}}" \
  "$IMAGE_TAG" 2>/dev/null || true)"
if [ "$loaded_fingerprint" != "$FINGERPRINT" ]; then
  echo "docker-test-lifecycle: pulled image failed fingerprint validation: $IMAGE_TAG" >&2
  exit 1
fi
printf '%s\n' "$LEASE_VALUE" > "$LEASE_FILE.tmp.$$"
mv "$LEASE_FILE.tmp.$$" "$LEASE_FILE"
