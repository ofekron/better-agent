#!/usr/bin/env bash
# Canonical way to run backend/scripts pytest tests — for humans AND agents.
#
# Always runs inside docker/Dockerfile.test, never against a host venv
# (backend/.venv, backend/.venvs/<hash>) directly. This exists because those
# host venvs drift (different Python versions, different installed deps
# depending on which venv `run.sh` last activated) and multiple concurrent
# agents editing this repo were hitting each other's runtime state. The
# container pins Python 3.13 + backend/requirements*.txt exactly, so a run
# here is reproducible regardless of what's active on the host.
#
# Usage:
#   ./scripts/run-backend-tests.sh                      # test the working tree (uncommitted changes included)
#   ./scripts/run-backend-tests.sh --ref <git-ref>       # test an exact commit/tag/branch, ignoring local changes
#   ./scripts/run-backend-tests.sh -- -k some_test       # pass args through to `python -m pytest`
#   ./scripts/run-backend-tests.sh --ref HEAD~1 -- scripts/test_foo.py
#
# RUN_LLM_TESTS is forwarded only if already set in the calling shell — this
# script never sets it itself. Per repo policy, live-LLM test runs require
# the user's explicit per-run approval; setting it here would silently grant
# that approval on every invocation.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$REPO_ROOT/docker/Dockerfile.test"

REF=""
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF="$2"
      shift 2
      ;;
    --)
      shift
      PYTEST_ARGS=("$@")
      break
      ;;
    *)
      echo "run-backend-tests: unknown argument: $1 (pytest args must follow --)" >&2
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
run-backend-tests: docker is not installed or not on PATH.

This script intentionally does NOT auto-install Docker. Options:
  - macOS:  install Docker Desktop or `brew install --cask orbstack`
  - Linux:  follow https://docs.docker.com/engine/install/

Then re-run: ./scripts/run-backend-tests.sh
EOF
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "run-backend-tests: $DOCKERFILE not found — wrong checkout?" >&2
  exit 1
fi

if [ -n "$REF" ]; then
  COMMIT_SHA="$(git -C "$REPO_ROOT" rev-parse --verify "$REF")"
  IMAGE_TAG="better-agent-backend-tests:${COMMIT_SHA}"
  echo "run-backend-tests: building $IMAGE_TAG pinned to commit $COMMIT_SHA (ref: $REF)"
  git -C "$REPO_ROOT" archive "$COMMIT_SHA" \
    | docker build -f docker/Dockerfile.test -t "$IMAGE_TAG" -
else
  IMAGE_TAG="better-agent-backend-tests:worktree"
  echo "run-backend-tests: building $IMAGE_TAG from the working tree (uncommitted changes included)"
  docker build -f "$DOCKERFILE" -t "$IMAGE_TAG" "$REPO_ROOT"
fi

RUN_ARGS=(--rm)
if [ -n "${RUN_LLM_TESTS:-}" ]; then
  RUN_ARGS+=(-e "RUN_LLM_TESTS=${RUN_LLM_TESTS}")
fi

echo "run-backend-tests: running tests in $IMAGE_TAG"
docker run "${RUN_ARGS[@]}" "$IMAGE_TAG" "${PYTEST_ARGS[@]}"
