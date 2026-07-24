#!/usr/bin/env bash
# Run the clean-room install smoke test locally.
#
# Builds tests/install-smoke/Dockerfile against the current working tree.
# The build itself is the test — see the Dockerfile's header comment for
# exactly what it does and does not cover.
#
# Usage:
#   ./scripts/test-install.sh
#
# This is what CI runs (.github/workflows/installation-mode-parity.yml,
# job install-smoke). Keep the two in lockstep.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$REPO_ROOT/tests/install-smoke/Dockerfile"
IMAGE_TAG="${BA_INSTALL_SMOKE_TAG:-better-agent-install-smoke:local}"

if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
test-install: docker is not installed or not on PATH.

This script intentionally does NOT auto-install Docker — surprising the user
with a heavyweight install is worse than failing fast. Options:

  - macOS:  install Docker Desktop or `brew install --cask orbstack`
  - Linux:  follow https://docs.docker.com/engine/install/

Then re-run: ./scripts/test-install.sh
EOF
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "test-install: $DOCKERFILE not found — wrong checkout?" >&2
  exit 1
fi

echo "test-install: building $IMAGE_TAG from $REPO_ROOT"
echo "test-install: dockerfile = $DOCKERFILE"

docker build \
  --pull \
  -f "$DOCKERFILE" \
  -t "$IMAGE_TAG" \
  "$REPO_ROOT"

echo "test-install: OK (image: $IMAGE_TAG)"
