#!/usr/bin/env bash

DOCKER_TEST_BUILDER="better-agent-tests"
DOCKER_TEST_LABEL_PREFIX="com.better-agent.test"
DOCKER_TEST_DEFAULT_REF_IMAGE_LIMIT=3
DOCKER_TEST_DEFAULT_DEPS_IMAGE_LIMIT=2
DOCKER_TEST_DEFAULT_CACHE_LIMIT="10GB"
# Shared singleton local registry used to hand images from the isolated
# docker-container builder to the engine's local image store, replacing the
# wedge-prone `buildx --load` tarball stream. Long-lived like the builder
# itself; storage is a disposable cache bounded by
# BETTER_AGENT_DOCKER_REGISTRY_CACHE_LIMIT (recreated, never GC'd in place).
DOCKER_TEST_REGISTRY="better-agent-test-registry"
DOCKER_TEST_REGISTRY_NETWORK="better-agent-test-net"
DOCKER_TEST_REGISTRY_VOLUME="better-agent-test-registry-data"
DOCKER_TEST_DEFAULT_REGISTRY_PORT=5581
DOCKER_TEST_DEFAULT_REGISTRY_CACHE_LIMIT="10GB"
DOCKER_TEST_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_TEST_MATERIALIZER="$DOCKER_TEST_LIB_DIR/docker-test-materialize-image.sh"
DOCKER_TEST_EXIT_NORMALIZER="$DOCKER_TEST_LIB_DIR/docker-test-normalize-exit.sh"

docker_test_process_start() {
  ps -p "$1" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

docker_test_lifecycle_init() {
  DOCKER_TEST_FAMILY="$1"
  docker_test_require_local_daemon
  DOCKER_TEST_HOST="$(hostname)"
  DOCKER_TEST_OWNER_START="$(docker_test_process_start "$$")"
  [ -n "$DOCKER_TEST_HOST" ] || return 1
  [ -n "$DOCKER_TEST_OWNER_START" ] || return 1

  DOCKER_TEST_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/better-agent-docker.XXXXXX")"
  DOCKER_TEST_RUN_ID="$(basename "$DOCKER_TEST_RUN_DIR")-$$"
  DOCKER_TEST_CONTAINER_NAME="better-agent-test-${DOCKER_TEST_FAMILY}-${DOCKER_TEST_RUN_ID}"
  DOCKER_TEST_CIDFILE="$DOCKER_TEST_RUN_DIR/container.cid"
  DOCKER_TEST_IMAGE_LEASE=""
  DOCKER_TEST_CURRENT_CONTAINER=""
  trap 'docker_test_remove_current_container' EXIT
}

docker_test_require_local_daemon() {
  local endpoint="${DOCKER_HOST:-}"
  if [ -z "$endpoint" ]; then
    endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
  fi
  case "$endpoint" in
    unix://*|npipe://*) return 0 ;;
    *)
      echo "docker-test-lifecycle: coordinated materialization requires a local Docker daemon" >&2
      return 1
      ;;
  esac
}

docker_test_owner_is_alive() {
  local owner_pid="$1"
  local owner_start="$2"
  local current_start
  current_start="$(docker_test_process_start "$owner_pid")"
  [ -n "$current_start" ] && [ "$current_start" = "$owner_start" ]
}

docker_test_reap_orphans() {
  local container_id owner host_pid owner_start containers
  # `for x in $(cmd)` discards cmd's own exit status (set -e does not check
  # command substitutions used as a word-list source) — capture explicitly
  # so a transient daemon error here is reported instead of silently
  # skipping orphan reaping.
  if ! containers="$(docker ps \
    --filter "label=${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
    --filter "label=${DOCKER_TEST_LABEL_PREFIX}.host=${DOCKER_TEST_HOST}" \
    --format '{{.ID}}')"; then
    echo "docker-test-lifecycle: could not list containers for orphan reaping (daemon busy?) — skipping this pass" >&2
    return 0
  fi
  for container_id in $containers; do
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

docker_test_registry_port() {
  printf '%s\n' "${BETTER_AGENT_DOCKER_REGISTRY_PORT:-$DOCKER_TEST_DEFAULT_REGISTRY_PORT}"
}

docker_test_size_to_bytes() {
  local raw="$1" number unit
  number="$(printf '%s' "$raw" | sed -E 's/^([0-9]+).*/\1/')"
  unit="$(printf '%s' "$raw" | sed -E 's/^[0-9]+//' | tr '[:lower:]' '[:upper:]')"
  case "$number" in
    ''|*[!0-9]*)
      echo "docker-test-lifecycle: invalid size value: $raw" >&2
      return 1
      ;;
  esac
  case "$unit" in
    ''|B) printf '%s\n' "$number" ;;
    K|KB) echo $((number * 1024)) ;;
    M|MB) echo $((number * 1024 * 1024)) ;;
    G|GB) echo $((number * 1024 * 1024 * 1024)) ;;
    T|TB) echo $((number * 1024 * 1024 * 1024 * 1024)) ;;
    *)
      echo "docker-test-lifecycle: unknown size unit in: $raw" >&2
      return 1
      ;;
  esac
}

docker_test_registry_volume_bytes() {
  docker volume inspect "$DOCKER_TEST_REGISTRY_VOLUME" >/dev/null 2>&1 || { echo 0; return 0; }
  docker run --rm -v "${DOCKER_TEST_REGISTRY_VOLUME}:/data:ro" alpine:3 du -sb /data 2>/dev/null \
    | awk '{print $1}'
}

# Registry storage is a disposable cache, never GC'd in place: once it
# exceeds the cap, delete and recreate container+volume. Must run under the
# builder lock (see docker_test_ensure_infra) so concurrent sessions never
# race the same recreation.
docker_test_ensure_registry_within_cap() {
  local cache_limit="${BETTER_AGENT_DOCKER_REGISTRY_CACHE_LIMIT:-$DOCKER_TEST_DEFAULT_REGISTRY_CACHE_LIMIT}"
  local limit_bytes used_bytes
  limit_bytes="$(docker_test_size_to_bytes "$cache_limit")" || return 1
  used_bytes="$(docker_test_registry_volume_bytes)"
  case "$used_bytes" in *[!0-9]*|'') used_bytes=0 ;; esac
  [ "$used_bytes" -le "$limit_bytes" ] && return 0
  echo "docker-test-lifecycle: test registry cache ${used_bytes}B exceeds cap ${cache_limit}; recreating" >&2
  docker rm -f "$DOCKER_TEST_REGISTRY" >/dev/null 2>&1 || true
  docker volume rm "$DOCKER_TEST_REGISTRY_VOLUME" >/dev/null 2>&1 || true
}

docker_test_ensure_network() {
  docker network inspect "$DOCKER_TEST_REGISTRY_NETWORK" >/dev/null 2>&1 && return 0
  docker network create \
    --label "${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
    "$DOCKER_TEST_REGISTRY_NETWORK" >/dev/null 2>&1 \
    || docker network inspect "$DOCKER_TEST_REGISTRY_NETWORK" >/dev/null 2>&1
}

docker_test_registry_running() {
  [ -n "$(docker ps -q --filter "name=^/${DOCKER_TEST_REGISTRY}$" --filter status=running)" ]
}

# Poll the registry's actual readiness (not a blind sleep): the port only
# becomes reachable once the registry process has bound it, so this is an
# event-driven wait on the real precondition, bounded to avoid hanging
# forever if the container never comes up.
docker_test_wait_for_registry_ready() {
  local port="$1" attempt=0
  local max="${BETTER_AGENT_DOCKER_REGISTRY_READY_ATTEMPTS:-30}"
  while [ "$attempt" -lt "$max" ]; do
    if command -v curl >/dev/null 2>&1; then
      curl -sf -o /dev/null "http://127.0.0.1:${port}/v2/" && return 0
    elif command -v nc >/dev/null 2>&1; then
      nc -z 127.0.0.1 "$port" 2>/dev/null && return 0
    else
      docker_test_registry_running && return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  return 1
}

# Must run under the builder lock (see docker_test_ensure_infra) so
# concurrent sessions never see a half-started registry.
docker_test_ensure_registry() {
  docker_test_ensure_network || return 1
  docker_test_ensure_registry_within_cap || return 1
  docker_test_registry_running && return 0
  docker rm -f "$DOCKER_TEST_REGISTRY" >/dev/null 2>&1 || true
  local port
  port="$(docker_test_registry_port)"
  if ! docker run -d \
    --name "$DOCKER_TEST_REGISTRY" \
    --network "$DOCKER_TEST_REGISTRY_NETWORK" \
    --restart unless-stopped \
    -p "127.0.0.1:${port}:5000" \
    -v "${DOCKER_TEST_REGISTRY_VOLUME}:/var/lib/registry" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
    --label "${DOCKER_TEST_LABEL_PREFIX}.role=registry" \
    registry:2 >/dev/null; then
    echo "docker-test-lifecycle: failed to start test registry '$DOCKER_TEST_REGISTRY' on port $port. Recovery: 'docker rm -f $DOCKER_TEST_REGISTRY && docker volume rm $DOCKER_TEST_REGISTRY_VOLUME' (recreated on demand); check the port isn't already bound." >&2
    return 1
  fi
  if ! docker_test_wait_for_registry_ready "$port"; then
    echo "docker-test-lifecycle: test registry did not become ready on port $port. Recovery: inspect 'docker logs $DOCKER_TEST_REGISTRY', then 'docker rm -f $DOCKER_TEST_REGISTRY && docker volume rm $DOCKER_TEST_REGISTRY_VOLUME' (recreated on demand)." >&2
    return 1
  fi
}

docker_test_builder_container_name() {
  printf 'buildx_buildkit_%s0' "$DOCKER_TEST_BUILDER"
}

docker_test_builder_on_network() {
  docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
    "$(docker_test_builder_container_name)" 2>/dev/null \
    | tr ' ' '\n' | grep -qx "$DOCKER_TEST_REGISTRY_NETWORK"
}

# Builder network attachment requires recreating the builder with the right
# --driver-opt. Must run under the builder lock (see docker_test_ensure_infra)
# and only recreates when the existing builder lacks the network config, so
# concurrent sessions never observe a half-configured builder.
docker_test_ensure_builder_on_network() {
  if docker buildx inspect "$DOCKER_TEST_BUILDER" >/dev/null 2>&1; then
    docker_test_builder_on_network && return 0
    echo "docker-test-lifecycle: recreating builder '$DOCKER_TEST_BUILDER' to attach the test registry network" >&2
    docker buildx rm "$DOCKER_TEST_BUILDER" >/dev/null 2>&1 || true
  fi
  docker buildx create --name "$DOCKER_TEST_BUILDER" --driver docker-container \
    --driver-opt "network=${DOCKER_TEST_REGISTRY_NETWORK}" >/dev/null 2>&1 \
    || { docker buildx inspect "$DOCKER_TEST_BUILDER" >/dev/null 2>&1 && docker_test_builder_on_network; }
}

# Ensures the registry and the network-attached builder together, in that
# order (the builder attaches to a network that must already exist). Callers
# invoke this under the builder lock so two concurrent sessions never race
# the same create-or-migrate decision.
docker_test_ensure_infra() {
  docker_test_ensure_registry || return 1
  docker_test_ensure_builder_on_network
}

docker_test_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
    return
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
    return
  fi
  echo "docker-test-lifecycle: sha256sum or shasum is required" >&2
  return 1
}

docker_test_platform() {
  docker version --format '{{.Server.Os}}/{{.Server.Arch}}'
}

docker_test_fingerprint() {
  local root="$1"
  shift
  (
    printf 'platform\0%s\0' "$(docker_test_platform)"
    find "$@" \( -type f -o -type l \) -print0 \
      | LC_ALL=C sort -z \
      | while IFS= read -r -d '' path; do
      printf 'path\0%s\0executable\0' "${path#"$root"/}"
      if [ -x "$path" ]; then printf '1\0'; else printf '0\0'; fi
      if [ -L "$path" ]; then
        printf 'symlink\0'
        readlink -n "$path"
        printf '\0'
      else
        printf 'file\0'
        docker_test_sha256 < "$path"
      fi
    done
  ) | docker_test_sha256
}

docker_test_snapshot_context() {
  local root="$1"
  local destination="$2"
  local input relative
  shift 2
  mkdir -p "$destination"
  for input in "$@"; do
    case "$input" in
      "$root"/*) relative="${input#"$root"/}" ;;
      *)
        echo "docker-test-lifecycle: snapshot input escapes repository: $input" >&2
        return 1
        ;;
    esac
    tar -C "$root" -cf - "$relative" | tar -C "$destination" -xf -
  done
}

docker_test_snapshot_git_ref() {
  local repository="$1"
  local commit_sha="$2"
  local destination="$3"
  mkdir -p "$destination"
  git -C "$repository" archive "$commit_sha" | tar -x -C "$destination"
}

docker_test_value_fingerprint() {
  printf 'platform\0%s\0value\0%s\0' "$(docker_test_platform)" "$1" \
    | docker_test_sha256
}

docker_test_materialize_image() {
  local fingerprint="$1"
  local image_tag="$2"
  local image_kind="${DOCKER_TEST_IMAGE_KIND:-deps}"
  shift 2

  docker_test_prepare_lock_root
  DOCKER_TEST_IMAGE_LEASE="$DOCKER_TEST_LEASE_DIR/$DOCKER_TEST_RUN_ID"
  local registry_port
  registry_port="$(docker_test_registry_port)"
  if docker_test_with_state_lock "$DOCKER_TEST_MATERIALIZER" check \
    "$DOCKER_TEST_BUILDER" "$DOCKER_TEST_REGISTRY" "$registry_port" "$DOCKER_TEST_REGISTRY_VOLUME" \
    "$DOCKER_TEST_FAMILY" "$image_kind" \
    "$fingerprint" "$image_tag" "$DOCKER_TEST_IMAGE_LEASE" \
    "$image_tag|$DOCKER_TEST_HOST|$$|$DOCKER_TEST_OWNER_START" "$@"; then
    return 0
  else
    local check_status=$?
    [ "$check_status" -eq 75 ] || return "$check_status"
  fi
  # Registry/builder-network create-or-migrate must be serialized under the
  # builder lock so concurrent sessions never see a half-configured builder
  # or a half-started registry.
  docker_test_with_builder_lock env \
    BETTER_AGENT_DOCKER_REGISTRY_PORT="${BETTER_AGENT_DOCKER_REGISTRY_PORT:-}" \
    BETTER_AGENT_DOCKER_REGISTRY_CACHE_LIMIT="${BETTER_AGENT_DOCKER_REGISTRY_CACHE_LIMIT:-}" \
    bash -c 'source "$1"; docker_test_ensure_infra' _ \
    "$DOCKER_TEST_LIB_DIR/docker-test-lifecycle.sh" || return 1
  docker_test_with_builder_lock "$DOCKER_TEST_MATERIALIZER" materialize \
    "$DOCKER_TEST_BUILDER" "$DOCKER_TEST_REGISTRY" "$registry_port" "$DOCKER_TEST_REGISTRY_VOLUME" \
    "$DOCKER_TEST_FAMILY" "$image_kind" \
    "$fingerprint" "$image_tag" "$DOCKER_TEST_IMAGE_LEASE" \
    "$image_tag|$DOCKER_TEST_HOST|$$|$DOCKER_TEST_OWNER_START" "$@"
}

docker_test_prepare_lock_root() {
  local uid
  uid="$(id -u)"
  DOCKER_TEST_LOCK_ROOT="/tmp/better-agent-docker-build-locks-$uid"
  if [ -L "$DOCKER_TEST_LOCK_ROOT" ]; then
    echo "docker-test-lifecycle: refusing symlink lock directory: $DOCKER_TEST_LOCK_ROOT" >&2
    return 1
  fi
  if [ -e "$DOCKER_TEST_LOCK_ROOT" ]; then
    [ -d "$DOCKER_TEST_LOCK_ROOT" ] && [ -O "$DOCKER_TEST_LOCK_ROOT" ] || {
      echo "docker-test-lifecycle: unsafe lock directory: $DOCKER_TEST_LOCK_ROOT" >&2
      return 1
    }
  else
    (umask 077 && mkdir "$DOCKER_TEST_LOCK_ROOT" 2>/dev/null) || {
      [ ! -L "$DOCKER_TEST_LOCK_ROOT" ] \
        && [ -d "$DOCKER_TEST_LOCK_ROOT" ] \
        && [ -O "$DOCKER_TEST_LOCK_ROOT" ] || return 1
    }
  fi
  chmod 700 "$DOCKER_TEST_LOCK_ROOT"
  DOCKER_TEST_LEASE_DIR="$DOCKER_TEST_LOCK_ROOT/leases"
  if [ -L "$DOCKER_TEST_LEASE_DIR" ]; then
    echo "docker-test-lifecycle: refusing symlink lease directory: $DOCKER_TEST_LEASE_DIR" >&2
    return 1
  fi
  if [ -e "$DOCKER_TEST_LEASE_DIR" ]; then
    [ -d "$DOCKER_TEST_LEASE_DIR" ] && [ -O "$DOCKER_TEST_LEASE_DIR" ] || return 1
  else
    (umask 077 && mkdir "$DOCKER_TEST_LEASE_DIR" 2>/dev/null) || {
      [ ! -L "$DOCKER_TEST_LEASE_DIR" ] \
        && [ -d "$DOCKER_TEST_LEASE_DIR" ] \
        && [ -O "$DOCKER_TEST_LEASE_DIR" ] || return 1
    }
  fi
}

docker_test_with_lock() {
  local lock_file="$1"
  local wait_mode="$2"
  shift 2
  docker_test_prepare_lock_root

  if command -v flock >/dev/null 2>&1; then
    if [ "$wait_mode" = try ]; then
      flock -E 75 -n "$lock_file" "$DOCKER_TEST_EXIT_NORMALIZER" "$@"
      return
    fi
    flock "$lock_file" "$@"
    return
  fi
  if command -v lockf >/dev/null 2>&1; then
    if [ "$wait_mode" = try ]; then
      lockf -s -t 0 -k "$lock_file" "$DOCKER_TEST_EXIT_NORMALIZER" "$@"
      return
    fi
    lockf -k "$lock_file" "$@"
    return
  fi
  echo "docker-test-lifecycle: flock or lockf is required for safe image materialization" >&2
  return 1
}

docker_test_with_builder_lock() {
  docker_test_prepare_lock_root
  docker_test_with_lock "$DOCKER_TEST_LOCK_ROOT/${DOCKER_TEST_BUILDER}.lock" wait "$@"
}

docker_test_try_with_builder_lock() {
  docker_test_prepare_lock_root
  docker_test_with_lock "$DOCKER_TEST_LOCK_ROOT/${DOCKER_TEST_BUILDER}.lock" try "$@"
}

docker_test_with_state_lock() {
  docker_test_prepare_lock_root
  docker_test_with_lock "$DOCKER_TEST_LOCK_ROOT/state.lock" wait "$@"
}

docker_test_remove_current_container() {
  if [ -n "${DOCKER_TEST_CURRENT_CONTAINER:-}" ]; then
    docker rm -f "$DOCKER_TEST_CURRENT_CONTAINER" >/dev/null 2>&1 || true
    DOCKER_TEST_CURRENT_CONTAINER=""
  fi
  if [ -n "${DOCKER_TEST_RUN_DIR:-}" ] && [ -d "$DOCKER_TEST_RUN_DIR" ]; then
    rm -rf "$DOCKER_TEST_RUN_DIR"
  fi
  if [ -n "${DOCKER_TEST_IMAGE_LEASE:-}" ]; then
    rm -f "$DOCKER_TEST_IMAGE_LEASE"
    DOCKER_TEST_IMAGE_LEASE=""
  fi
}

# Resource caps/priority for the pytest-running container (NOT the
# builder/registry infra, which is out of scope). Opt-in via env so local
# dev keeps today's behavior when a knob is unset. Prints one docker flag
# per line so callers can populate a plain bash array without namerefs or
# eval (this repo also targets bash 3.2 — macOS's default /bin/bash).
#
#   BETTER_AGENT_TEST_CPUS       -> --cpus=<value>
#                                    default "2", matching the cap this
#                                    container has always run under
#                                    (see ffc1caeea) — unset changes nothing.
#   BETTER_AGENT_TEST_MEMORY     -> --memory=<value> and
#                                    --memory-swap=<value> (same value, so a
#                                    capped container can't spill onto swap
#                                    and thrash instead of failing cleanly)
#                                    default unset (no flag), matching the
#                                    uncapped memory this container has
#                                    always run under.
#   BETTER_AGENT_TEST_CPU_SHARES -> --cpu-shares=<value>
#                                    default unset (no flag; docker's own
#                                    default weight of 1024 applies).
#                                    Deliberately relative-weight, not a
#                                    second hard ceiling: a lowered share
#                                    only costs the container CPU when
#                                    something else on the host is actually
#                                    contending for it. On an idle host it
#                                    can still burst up to its --cpus
#                                    ceiling, so this is what buys "fast
#                                    when idle, invisible when the user is
#                                    working" without a fixed low --cpus
#                                    value hurting cold-cache runs on an
#                                    otherwise-idle machine.
docker_test_resource_cap_args() {
  local cpus="${BETTER_AGENT_TEST_CPUS:-2}"
  local memory="${BETTER_AGENT_TEST_MEMORY:-}"
  local shares="${BETTER_AGENT_TEST_CPU_SHARES:-}"
  [ -n "$cpus" ] && printf -- '--cpus=%s\n' "$cpus"
  if [ -n "$memory" ]; then
    printf -- '--memory=%s\n' "$memory"
    printf -- '--memory-swap=%s\n' "$memory"
  fi
  [ -n "$shares" ] && printf -- '--cpu-shares=%s\n' "$shares"
  # Explicit: without this, the function's own exit status is that of the
  # `shares` test above, which is false (1) whenever the shares knob is
  # unset — under `set -e`, `x="$(docker_test_resource_cap_args)"` at a
  # caller's top level would then abort the whole script even though
  # nothing here failed.
  return 0
}

# pytest-xdist args for the PYTEST INVOCATION itself (distinct from
# docker_test_resource_cap_args above, which caps the CONTAINER). Takes the
# resolved worker count as $1 — "" (unset knob, the local-dev default) emits
# nothing, so a bare run stays single-process exactly as it always has.
#
# `--dist loadfile` is not optional once a worker count is set: it keeps
# every test in one file on the SAME xdist worker process. This suite has a
# documented history (three rounds of CI triage) of module-level state leaks
# — a fixture that mutates a shared module attribute, or an unscoped
# monkeypatch — bleeding from one test FILE into a LATER file collected in
# the same pytest process (see e.g. the stores.worker_store leak that caused
# 42% of one CI run's failures). `--dist loadfile` cannot make that class of
# bug worse: each worker is its own OS process, so two files assigned to
# different workers cannot share module state at all — strictly stronger
# isolation than today's single sequential process, where every file shares
# one process's module table for the whole run. `loadscope`/`load` are never
# used here because either could split ONE file's tests across workers,
# which the per-module home-isolation fixtures in backend/scripts/conftest.py
# assume never happens (they assume serial, single-worker execution within a
# module).
docker_test_xdist_pytest_args() {
  local workers="$1"
  [ -n "$workers" ] || return 0
  printf -- '-n\n%s\n--dist\nloadfile\n' "$workers"
  return 0
}

# BETTER_AGENT_TEST_CHOWN forwards the invoking host's uid:gid into the
# container as an env var, for docker/entrypoint-test.sh to chown its
# bind-mounted /repo back to the invoking user after the test run (see that
# script's header for the full rationale). Same one-flag-per-line contract
# as docker_test_resource_cap_args, so callers populate RUN_ARGS the same
# way (a `while read` loop into a bash 3.2-safe array, no namerefs/eval).
#
# Native Linux Docker hosts (self-hosted CI runners, plain docker-ce) share
# the bind mount's filesystem/inode ownership 1:1 with the container's UID,
# so files docker/Dockerfile.test's root-owned entrypoint writes into /repo
# (junit XML, .pytest_cache) come out root-owned on the host too — the next
# CI job's actions/checkout can't clean those as the unprivileged `runner`
# user. Docker Desktop/OrbStack's macOS VM remaps bind-mount ownership to
# the invoking host user regardless of the in-container UID, so this must
# stay a deliberate no-op there: unconditionally forwarding it would add an
# always-needless chown -R to every local dev run for zero benefit.
docker_test_chown_env_args() {
  [ "$(uname -s)" = Linux ] || return 0
  printf -- '-e\n'
  printf -- 'BETTER_AGENT_TEST_CHOWN=%s:%s\n' "$(id -u)" "$(id -g)"
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

docker_test_prune_images_for_kind() {
  local image_kind="$1"
  local limit="$2"
  local index=0 line image_id seen=" " protected=" " lease lease_data
  case "$limit" in *[!0-9]*|'') return 1 ;; esac

  docker_test_prepare_lock_root
  for lease in "$DOCKER_TEST_LEASE_DIR"/*; do
    [ -f "$lease" ] || continue
    lease_data="$(cat "$lease" 2>/dev/null || true)"
    IFS='|' read -r image_tag host owner_pid owner_start <<EOF
$lease_data
EOF
    case "$owner_pid" in *[!0-9]*|'') rm -f "$lease"; continue ;; esac
    if [ "$host" = "$DOCKER_TEST_HOST" ] \
      && docker_test_owner_is_alive "$owner_pid" "$owner_start"; then
      image_id="$(docker image inspect --format '{{.Id}}' "$image_tag" 2>/dev/null || true)"
      [ -n "$image_id" ] && protected="$protected$image_id "
    else
      rm -f "$lease"
    fi
  done

  while IFS= read -r line; do
    image_id="${line#*|}"
    [ -n "$image_id" ] || continue
    case "$seen" in *" $image_id "*) continue ;; esac
    seen="$seen$image_id "
    index=$((index + 1))
    [ "$index" -le "$limit" ] && continue
    case "$protected" in *" $image_id "*) continue ;; esac
    if [ -z "$(docker ps -aq --filter "ancestor=$image_id")" ]; then
      docker image rm "$image_id" >/dev/null 2>&1 || true
    fi
  done <<EOF
$(docker image ls \
  --filter "label=${DOCKER_TEST_LABEL_PREFIX}.owner=true" \
  --filter "label=${DOCKER_TEST_LABEL_PREFIX}.family=${DOCKER_TEST_FAMILY}" \
  --filter "label=${DOCKER_TEST_LABEL_PREFIX}.kind=${image_kind}" \
  --format '{{.CreatedAt}}|{{.ID}}' | sort -r)
EOF
}

docker_test_prune_images() {
  local ref_limit="${BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT:-$DOCKER_TEST_DEFAULT_REF_IMAGE_LIMIT}"
  local deps_limit="${BETTER_AGENT_DOCKER_DEPS_IMAGE_LIMIT:-$DOCKER_TEST_DEFAULT_DEPS_IMAGE_LIMIT}"
  docker_test_prune_images_for_kind ref "$ref_limit"
  docker_test_prune_images_for_kind deps "$deps_limit"
}

docker_test_prune_build_cache_unlocked() {
  local cache_limit="${BETTER_AGENT_DOCKER_CACHE_LIMIT:-$DOCKER_TEST_DEFAULT_CACHE_LIMIT}"
  docker buildx inspect "$DOCKER_TEST_BUILDER" >/dev/null 2>&1 || return 0
  [ -n "$(docker ps -q --filter "name=buildx_buildkit_${DOCKER_TEST_BUILDER}")" ] \
    || return 0
  docker buildx prune --builder "$DOCKER_TEST_BUILDER" \
    --force --max-used-space "$cache_limit"
}

docker_test_prune_build_cache() {
  docker_test_with_builder_lock env \
    BETTER_AGENT_DOCKER_CACHE_LIMIT="${BETTER_AGENT_DOCKER_CACHE_LIMIT:-}" \
    bash -c 'source "$1"; docker_test_prune_build_cache_unlocked' _ \
    "$DOCKER_TEST_LIB_DIR/docker-test-lifecycle.sh"
}

docker_test_cleanup_locked() {
  docker_test_with_state_lock env \
    DOCKER_TEST_FAMILY="$DOCKER_TEST_FAMILY" \
    DOCKER_TEST_HOST="$DOCKER_TEST_HOST" \
    BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT="${BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT:-}" \
    BETTER_AGENT_DOCKER_DEPS_IMAGE_LIMIT="${BETTER_AGENT_DOCKER_DEPS_IMAGE_LIMIT:-}" \
    bash -c 'source "$1"; docker_test_prune_images' _ \
    "$DOCKER_TEST_LIB_DIR/docker-test-lifecycle.sh" || return
  docker_test_prune_build_cache_unlocked
}

docker_test_cleanup() {
  local cleanup_status
  if docker_test_try_with_builder_lock env \
    DOCKER_TEST_FAMILY="$DOCKER_TEST_FAMILY" \
    DOCKER_TEST_HOST="$DOCKER_TEST_HOST" \
    BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT="${BETTER_AGENT_DOCKER_REF_IMAGE_LIMIT:-}" \
    BETTER_AGENT_DOCKER_DEPS_IMAGE_LIMIT="${BETTER_AGENT_DOCKER_DEPS_IMAGE_LIMIT:-}" \
    BETTER_AGENT_DOCKER_CACHE_LIMIT="${BETTER_AGENT_DOCKER_CACHE_LIMIT:-}" \
    bash -c 'source "$1"; docker_test_cleanup_locked' _ \
    "$DOCKER_TEST_LIB_DIR/docker-test-lifecycle.sh"; then
    return 0
  else
    cleanup_status=$?
  fi
  if [ "$cleanup_status" -eq 75 ]; then
    echo "docker-test-lifecycle: cleanup deferred while builder is busy" >&2
    return 0
  fi
  echo "docker-test-lifecycle: cleanup failed with status $cleanup_status" >&2
  return "$cleanup_status"
}
