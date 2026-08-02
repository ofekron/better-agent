#!/usr/bin/env bash

DOCKER_TEST_BUILDER="better-agent-tests"
DOCKER_TEST_LABEL_PREFIX="com.better-agent.test"
DOCKER_TEST_DEFAULT_REF_IMAGE_LIMIT=3
DOCKER_TEST_DEFAULT_CACHE_LIMIT="10GB"

docker_test_process_start() {
  ps -p "$1" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

docker_test_lifecycle_init() {
  DOCKER_TEST_FAMILY="$1"
  DOCKER_TEST_HOST="$(hostname)"
  DOCKER_TEST_OWNER_START="$(docker_test_process_start "$$")"
  [ -n "$DOCKER_TEST_HOST" ] || return 1
  [ -n "$DOCKER_TEST_OWNER_START" ] || return 1

  DOCKER_TEST_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/better-agent-docker.XXXXXX")"
  DOCKER_TEST_RUN_ID="$(basename "$DOCKER_TEST_RUN_DIR")-$$"
  DOCKER_TEST_CONTAINER_NAME="better-agent-test-${DOCKER_TEST_FAMILY}-${DOCKER_TEST_RUN_ID}"
  DOCKER_TEST_CIDFILE="$DOCKER_TEST_RUN_DIR/container.cid"
  DOCKER_TEST_CURRENT_CONTAINER=""
  trap 'docker_test_remove_current_container' EXIT
}

docker_test_owner_is_alive() {
  local owner_pid="$1"
  local owner_start="$2"
  local current_start
  current_start="$(docker_test_process_start "$owner_pid")"
  [ -n "$current_start" ] && [ "$current_start" = "$owner_start" ]
}

docker_test_reap_orphans() {
  local container_id owner host_pid owner_start
  for container_id in $(docker ps \
    --filter "label=${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
    --filter "label=${DOCKER_TEST_LABEL_PREFIX}.host=${DOCKER_TEST_HOST}" \
    --format '{{.ID}}'); do
    owner="$(docker inspect --format \
      "{{index .Config.Labels \"${DOCKER_TEST_LABEL_PREFIX}.host\"}}|{{index .Config.Labels \"${DOCKER_TEST_LABEL_PREFIX}.owner-pid\"}}|{{index .Config.Labels \"${DOCKER_TEST_LABEL_PREFIX}.owner-start\"}}" \
      "$container_id" 2>/dev/null || true)"
    IFS='|' read -r host_pid owner_pid owner_start <<EOF
$owner
EOF
    [ "$host_pid" = "$DOCKER_TEST_HOST" ] || continue
    case "$owner_pid" in *[!0-9]*|'') continue ;; esac
    [ -n "$owner_start" ] || continue
    if ! docker_test_owner_is_alive "$owner_pid" "$owner_start"; then
      docker rm -f "$container_id" >/dev/null 2>&1 \
        || echo "docker-test-lifecycle: could not remove orphan $container_id" >&2
    fi
  done
}

docker_test_ensure_builder() {
  if docker buildx inspect "$DOCKER_TEST_BUILDER" >/dev/null 2>&1; then
    return 0
  fi
  docker buildx create --name "$DOCKER_TEST_BUILDER" --driver docker-container >/dev/null 2>&1 \
    || docker buildx inspect "$DOCKER_TEST_BUILDER" >/dev/null 2>&1
}

docker_test_build() {
  local image_kind="${DOCKER_TEST_IMAGE_KIND:-deps}"
  docker_test_ensure_builder
  docker buildx build --builder "$DOCKER_TEST_BUILDER" --load \
    --label "${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.family=${DOCKER_TEST_FAMILY}" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.kind=${image_kind}" \
    "$@"
}

docker_test_remove_current_container() {
  if [ -n "${DOCKER_TEST_CURRENT_CONTAINER:-}" ]; then
    docker rm -f "$DOCKER_TEST_CURRENT_CONTAINER" >/dev/null 2>&1 || true
    DOCKER_TEST_CURRENT_CONTAINER=""
  fi
  if [ -n "${DOCKER_TEST_RUN_DIR:-}" ] && [ -d "$DOCKER_TEST_RUN_DIR" ]; then
    rm -rf "$DOCKER_TEST_RUN_DIR"
  fi
}

docker_test_run() {
  local status
  DOCKER_TEST_CURRENT_CONTAINER="$DOCKER_TEST_CONTAINER_NAME"
  trap 'docker_test_remove_current_container' EXIT
  trap 'docker_test_remove_current_container; exit 129' HUP
  trap 'docker_test_remove_current_container; exit 130' INT
  trap 'docker_test_remove_current_container; exit 143' TERM
  if docker run \
    --name "$DOCKER_TEST_CONTAINER_NAME" \
    --cidfile "$DOCKER_TEST_CIDFILE" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.family=${DOCKER_TEST_FAMILY}" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.run=${DOCKER_TEST_RUN_ID}" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.host=${DOCKER_TEST_HOST}" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.owner-pid=$$" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.owner-start=${DOCKER_TEST_OWNER_START}" \
    "$@"; then
    status=0
  else
    status=$?
  fi
  docker_test_remove_current_container
  trap - EXIT HUP INT TERM
  return "$status"
}

docker_test_prune_images() {
  local limit="${BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT:-$DOCKER_TEST_DEFAULT_REF_IMAGE_LIMIT}"
  local index=0 line image_id seen=" "
  case "$limit" in *[!0-9]*|'') return 1 ;; esac

  while IFS= read -r line; do
    image_id="${line#*|}"
    [ -n "$image_id" ] || continue
    case "$seen" in *" $image_id "*) continue ;; esac
    seen="$seen$image_id "
    index=$((index + 1))
    [ "$index" -le "$limit" ] && continue
    if [ -z "$(docker ps -aq --filter "ancestor=$image_id")" ]; then
      docker image rm "$image_id" >/dev/null 2>&1 || true
    fi
  done <<EOF
$(docker image ls \
  --filter "label=${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
  --filter "label=${DOCKER_TEST_LABEL_PREFIX}.family=${DOCKER_TEST_FAMILY}" \
  --filter "label=${DOCKER_TEST_LABEL_PREFIX}.kind=ref" \
  --format '{{.CreatedAt}}|{{.ID}}' | sort -r)
EOF
}

docker_test_prune_build_cache() {
  local cache_limit="${BETTER_AGENT_DOCKER_CACHE_LIMIT:-$DOCKER_TEST_DEFAULT_CACHE_LIMIT}"
  docker buildx inspect "$DOCKER_TEST_BUILDER" >/dev/null 2>&1 || return 0
  docker buildx prune --builder "$DOCKER_TEST_BUILDER" --force --max-used-space "$cache_limit"
}

docker_test_cleanup() {
  docker_test_prune_images || echo "docker-test-lifecycle: image cleanup skipped" >&2
  docker_test_prune_build_cache || echo "docker-test-lifecycle: cache cleanup skipped" >&2
}
