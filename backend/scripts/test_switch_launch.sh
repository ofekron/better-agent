#!/usr/bin/env bash
# Locks the line-switch launch decision: the active checkout's frontend must be
# built synchronously when it has no dist (cold clone or a switch to a
# never-built line), or the backend crashes at import in mount_frontend().
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=scripts/switch_launch.sh
source "$REPO/scripts/switch_launch.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Cold checkout: no dist/index.html -> must need a synchronous build.
cold="$tmp/cold"
mkdir -p "$cold/frontend"
if ! active_frontend_needs_build "$cold"; then
  echo "FAIL: a cold checkout (no dist) must need a synchronous build"
  exit 1
fi

# Warm checkout: dist/index.html present -> must NOT need a synchronous build,
# so it keeps the fast serve-previous-while-rebuilding path.
warm="$tmp/warm"
mkdir -p "$warm/frontend/dist"
: > "$warm/frontend/dist/index.html"
if active_frontend_needs_build "$warm"; then
  echo "FAIL: a warm checkout (dist present) must not need a synchronous build"
  exit 1
fi

echo "OK test_switch_launch"
