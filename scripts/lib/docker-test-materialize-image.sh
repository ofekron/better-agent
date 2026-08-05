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

docker buildx build --builder "$BUILDER" --load \
  --label "${LABEL_PREFIX}.owner=true" \
  --label "${LABEL_PREFIX}.family=${FAMILY}" \
  --label "${LABEL_PREFIX}.kind=${KIND}" \
  --label "${LABEL_PREFIX}.fingerprint=${FINGERPRINT}" \
  "$@"

loaded_fingerprint="$(docker image inspect \
  --format "{{index .Config.Labels \"${LABEL_PREFIX}.fingerprint\"}}" \
  "$IMAGE_TAG" 2>/dev/null || true)"
if [ "$loaded_fingerprint" != "$FINGERPRINT" ]; then
  echo "docker-test-lifecycle: loaded image failed fingerprint validation: $IMAGE_TAG" >&2
  exit 1
fi
printf '%s\n' "$LEASE_VALUE" > "$LEASE_FILE.tmp.$$"
mv "$LEASE_FILE.tmp.$$" "$LEASE_FILE"
