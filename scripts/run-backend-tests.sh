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
#   ./scripts/run-backend-tests.sh --parallel             # run via pytest-xdist, workers = nproc
#   ./scripts/run-backend-tests.sh --parallel 4            # ...with an explicit worker count
#
# --parallel is opt-in, never the default: backend/scripts integration tests
# spawn real subprocesses against fixed resources (ports, temp homes) that
# were never audited for cross-worker safety under pytest-xdist. Forcing it
# on by default would trade a slow-but-correct suite for a fast-but-flaky
# one — pass --parallel yourself once you've confirmed the target subset is
# parallel-safe (e.g. -k a single isolated module).
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
# ordering). The working-tree path (no --ref) builds only the `deps` target
# (interpreter + deps, no source) and bind-mounts the live repo over /repo at
# `docker run` time, so a source-only edit costs zero image rebuild/export —
# previously every invocation re-baked + re-exported a full source layer even
# though only the pytest run itself needed the fresh code. The --ref path
# still builds the `full` target (deps + COPY) from a `git archive` of the
# pinned commit, since that path must test a frozen tree, not a live mount.

set -euo pipefail

# BuildKit is required for docker/Dockerfile.test's pip cache mount
# (`RUN --mount=type=cache`). Modern Docker Desktop/OrbStack default to it,
# but set it explicitly so this also works on older or reconfigured daemons.
export DOCKER_BUILDKIT=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$REPO_ROOT/docker/Dockerfile.test"
# shellcheck source=lib/docker-test-lifecycle.sh
source "$HERE/lib/docker-test-lifecycle.sh"

REF=""
COVERAGE_DIR=""
PARALLEL_WORKERS=""
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
    --parallel)
      # --parallel [N]: same optional-value guard as --coverage. Bare
      # --parallel maps to pytest-xdist's `-n auto` (one worker per core).
      if [ "${2:-}" = "" ] || [ "${2#-}" != "${2:-}" ]; then
        PARALLEL_WORKERS="auto"
        shift 1
      else
        PARALLEL_WORKERS="$2"
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

docker_test_lifecycle_init backend
docker_test_reap_orphans
docker_test_cleanup

BIND_MOUNT_REPO=0
if [ -n "$REF" ]; then
  COMMIT_SHA="$(git -C "$REPO_ROOT" rev-parse --verify "$REF")"
  IMAGE_TAG="better-agent-backend-tests:${COMMIT_SHA}"
  echo "run-backend-tests: building $IMAGE_TAG pinned to commit $COMMIT_SHA (ref: $REF)"
  DOCKER_TEST_IMAGE_KIND=ref
  git -C "$REPO_ROOT" archive "$COMMIT_SHA" \
    | docker_test_build -f docker/Dockerfile.test --target full -t "$IMAGE_TAG" -
else
  IMAGE_TAG="better-agent-backend-tests:deps"
  BIND_MOUNT_REPO=1
  echo "run-backend-tests: building $IMAGE_TAG (deps only; live working tree is bind-mounted at run time)"
  DOCKER_TEST_IMAGE_KIND=deps
  docker_test_build -f "$DOCKERFILE" --target deps -t "$IMAGE_TAG" "$REPO_ROOT"
fi

RUN_ARGS=(--rm --cpus=2)
if [ -n "${RUN_LLM_TESTS:-}" ]; then
  RUN_ARGS+=(-e "RUN_LLM_TESTS=${RUN_LLM_TESTS}")
fi

if [ "$BIND_MOUNT_REPO" = "1" ]; then
  # Working-tree path: the `deps` image has no source baked in. Bind-mount
  # the live repo over /repo so pytest sees uncommitted edits with zero
  # rebuild — this also makes .pytest_cache read/write the host path
  # directly, so no separate cache volume is needed (unlike the `full`/--ref
  # path below, whose image has its own baked-in, non-host /repo/backend).
  RUN_ARGS+=(-v "$REPO_ROOT:/repo")
else
  # `--ref` path: the `full` image bakes in a frozen `git archive` snapshot,
  # not the host repo, so its /repo/backend/.pytest_cache isn't the host
  # path — mount the host's cache dir explicitly to still get incremental
  # --lf/--ff behavior instead of losing it to `--rm` every run.
  PYTEST_CACHE_DIR="$REPO_ROOT/backend/.pytest_cache"
  mkdir -p "$PYTEST_CACHE_DIR"
  RUN_ARGS+=(-v "$PYTEST_CACHE_DIR:/repo/backend/.pytest_cache")
fi

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

# pytest-cov ships built-in support for combining coverage data across
# pytest-xdist workers, so --parallel + --coverage compose with no extra
# config beyond both being present.
if [ -n "$PARALLEL_WORKERS" ]; then
  PYTEST_ARGS+=(-n "$PARALLEL_WORKERS")
fi

echo "run-backend-tests: running tests in $IMAGE_TAG"
TEST_STATUS=0
docker_test_run "${RUN_ARGS[@]}" "$IMAGE_TAG" "${PYTEST_ARGS[@]}" || TEST_STATUS=$?
docker_test_cleanup
exit "$TEST_STATUS"
