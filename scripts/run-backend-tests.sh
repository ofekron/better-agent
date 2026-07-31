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
#   ./scripts/run-backend-tests.sh --coverage            # measure coverage, write reports to ./coverage-backend/
#   ./scripts/run-backend-tests.sh --coverage /tmp/cov   # ...to a custom dir
#
# Coverage (--coverage) mounts an output dir into the container (which runs
# --rm, so reports written inside the layer would be lost) and appends
# pytest-cov args. Scope/omit rules live in backend/pyproject.toml's
# [tool.coverage.*], so the same scope is measured regardless of caller.
#
# RUN_LLM_TESTS is forwarded only if already set in the calling shell — this
# script never sets it itself. Per repo policy, live-LLM test runs require
# the user's explicit per-run approval; setting it here would silently grant
# that approval on every invocation.
#
# Speed: Docker layer caching means the apt/pip layers only re-run when
# backend/requirements*.txt change (see docker/Dockerfile.test's COPY
# ordering) — a plain code-change re-run only re-executes the fast COPY-source
# layer plus pytest itself, not a full environment rebuild.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$REPO_ROOT/docker/Dockerfile.test"

REF=""
COVERAGE_DIR=""
PYTEST_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF="$2"
      shift 2
      ;;
    --coverage)
      # --coverage [DIR]: use the default dir unless an explicit path is given.
      # Guard so `--coverage --` (DIR omitted) doesn't swallow the separator.
      if [ "${2:-}" = "" ] || [ "${2#-}" != "${2:-}" ]; then
        COVERAGE_DIR="$REPO_ROOT/coverage-backend"
        shift 1
      else
        COVERAGE_DIR="$2"
        shift 2
      fi
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

# Auto-detect the Docker daemon's native arch and only force linux/amd64 when
# it's arm64 — pyxdelta has no linux/arm64 wheel on PyPI, and its sdist is
# missing xdelta/xdelta3/xdelta3.c (upstream packaging bug: only headers are
# shipped), so a native arm64 build (e.g. Apple Silicon Docker
# Desktop/OrbStack) fails with "fatal error: xdelta3.c: No such file or
# directory". On an x86_64 daemon (matches CI: GitHub Actions runners are
# x86_64) the native build already has the wheel available, so building
# natively is both correct and avoids unnecessary QEMU emulation.
DOCKER_ARCH="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || true)"
case "$DOCKER_ARCH" in
  arm64|aarch64)
    PLATFORM_ARGS=(--platform linux/amd64)
    ;;
  *)
    PLATFORM_ARGS=()
    ;;
esac

if [ -n "$REF" ]; then
  COMMIT_SHA="$(git -C "$REPO_ROOT" rev-parse --verify "$REF")"
  IMAGE_TAG="better-agent-backend-tests:${COMMIT_SHA}"
  echo "run-backend-tests: building $IMAGE_TAG pinned to commit $COMMIT_SHA (ref: $REF)"
  git -C "$REPO_ROOT" archive "$COMMIT_SHA" \
    | docker build "${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"}" -f docker/Dockerfile.test -t "$IMAGE_TAG" -
else
  IMAGE_TAG="better-agent-backend-tests:worktree"
  echo "run-backend-tests: building $IMAGE_TAG from the working tree (uncommitted changes included)"
  docker build "${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"}" -f "$DOCKERFILE" -t "$IMAGE_TAG" "$REPO_ROOT"
fi

# ${arr[@]+"${arr[@]}"} (not plain "${arr[@]}") because macOS's default
# /bin/bash is 3.2, where an empty array under `set -u` throws "unbound
# variable" on plain expansion — this guard is a no-op on bash >=4.4.
RUN_ARGS=(--rm "${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"}")
if [ -n "${RUN_LLM_TESTS:-}" ]; then
  RUN_ARGS+=(-e "RUN_LLM_TESTS=${RUN_LLM_TESTS}")
fi

# `--rm` throws away the container's writable layer on exit, which would
# otherwise reset pytest's own incremental cache (.pytest_cache, used by
# --lf/--ff) on every invocation. Persist it on the host in the same place a
# native `pytest` run would write it, so repeat runs stay incremental. Already
# excluded from the build context (.dockerignore) and self-ignored by git
# (pytest writes its own .gitignore into this directory).
PYTEST_CACHE_DIR="$REPO_ROOT/backend/.pytest_cache"
mkdir -p "$PYTEST_CACHE_DIR"
RUN_ARGS+=(-v "$PYTEST_CACHE_DIR:/repo/backend/.pytest_cache")

# Coverage: mount the output dir (the container runs --rm, so reports written
# to its layer would be discarded) and append pytest-cov args. Scope/omit is
# driven by backend/pyproject.toml, so --cov needs no explicit source.
if [ -n "$COVERAGE_DIR" ]; then
  # docker -v needs an absolute path; resolve or create it.
  case "$COVERAGE_DIR" in
    /*) ;;
    *) COVERAGE_DIR="$REPO_ROOT/$COVERAGE_DIR" ;;
  esac
  mkdir -p "$COVERAGE_DIR"
  RUN_ARGS+=(-v "$COVERAGE_DIR:/coverage")
  # += avoids reading PYTEST_ARGS (empty-array read trips set -u on bash 3.2).
  PYTEST_ARGS+=(
    --cov
    --cov-report=term-missing
    --cov-report="json:/coverage/coverage.json"
    --cov-report="html:/coverage/html"
    --cov-config=/repo/backend/pyproject.toml
  )
fi

echo "run-backend-tests: running tests in $IMAGE_TAG"
docker run "${RUN_ARGS[@]}" "$IMAGE_TAG" "${PYTEST_ARGS[@]}"
