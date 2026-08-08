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
# --parallel (or its env-var equivalent BETTER_AGENT_TEST_XDIST, read as
# --parallel's default so CI can opt in without a CLI flag — see the
# per-runner env in .github/workflows/backend-tests-selfhosted.yml) is opt-in
# for LOCAL invocations, never the default: an unset knob keeps today's
# sequential single-process behavior unconditionally. CI itself now opts in
# by default because `--dist loadfile` (see docker_test_xdist_pytest_args in
# lib/docker-test-lifecycle.sh) keeps every test in one file on the SAME
# worker PROCESS — file-level isolation is the property backend/scripts
# integration tests actually rely on (temp homes engaged per module, fixed
# ports bound with port=0 for ephemeral assignment), and splitting work
# across worker processes cannot weaken that: two files on two different
# workers share no process state at all, which is strictly SAFER than
# today's single process where every file already shares one process's
# module table for the whole run. What stays unvetted is splitting a single
# file's own tests across workers (loadscope/load) — never used here.
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
#
# Resource caps on the test container itself (docker_test_resource_cap_args
# in lib/docker-test-lifecycle.sh): BETTER_AGENT_TEST_CPUS (default 2, same
# as always), BETTER_AGENT_TEST_MEMORY (+ matching --memory-swap; default
# unset/uncapped, same as always), BETTER_AGENT_TEST_CPU_SHARES (default
# unset — docker's normal 1024 weight). All are no-ops for local dev unless
# set; CI sets them per self-hosted runner in
# .github/workflows/backend-tests-selfhosted.yml.
#
# Bind-mount ownership on native Linux Docker hosts (docker_test_chown_env_args
# in lib/docker-test-lifecycle.sh): the working-tree path's /repo bind mount
# shares host filesystem ownership 1:1 with the container's UID, and the test
# image runs as root — so a native-Linux run forwards this host's uid:gid as
# BETTER_AGENT_TEST_CHOWN, and docker/entrypoint-test.sh chowns /repo back
# after the test run so root-owned junit/cache files don't break the next CI
# job's checkout. Automatic (uname -s = Linux), not opt-in; a no-op on
# macOS/Docker Desktop, whose VM already remaps bind-mount ownership.
#
# Per-test-file timing telemetry: pass `-- --junitxml=<path>` (plain pytest
# passthrough, no dedicated flag). For the default working-tree path above,
# /repo is a live bind-mount of this repo, so a relative --junitxml path
# lands directly on the host under backend/ once the container exits — no
# extra mount needed. The --ref path's `full` image has no such bind mount
# (its /repo is a frozen `git archive` snapshot baked into the image), so
# --junitxml there would be written inside the --rm container and lost;
# nothing in this repo currently combines --ref with --junitxml.
#
# Per-test timeout (BETTER_AGENT_TEST_TIMEOUT, seconds): forwarded as
# `--timeout=<value> --timeout-method=signal` (pytest-timeout, see
# backend/requirements-test.txt) ahead of any user-supplied pytest args, so
# an explicit `-- --timeout=...` still wins. Unset by default — local runs
# keep today's no-timeout behavior; CI sets it to 120s via
# .github/workflows/backend-tests-selfhosted.yml's job env, not hardcoded
# here, so this script's own default never changes for local dev. signal
# over thread: the test image is always Linux, and pytest-timeout's thread
# method can't safely interrupt a hung test at all (its own docs: on
# timeout it dumps thread stacks and hard-kills the whole pytest process,
# losing every remaining test in the run) — signal fails just the one
# hung test via SIGALRM and lets the suite continue, which is what we
# want. This was the exact gap in the 2026-08-07 incident: a single hung
# test burned the full ~100-minute runner budget with zero diagnostic
# because nothing bounded it.

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
# BETTER_AGENT_TEST_XDIST is --parallel's default so CI can opt in via env
# (matching the BETTER_AGENT_TEST_CPUS/_MEMORY/_TIMEOUT pattern already used
# for per-runner knobs) without needing a CLI flag; an explicit --parallel
# below still overrides it for manual invocations. Unset in both places
# leaves this empty, which is the local-dev default (sequential).
PARALLEL_WORKERS="${BETTER_AGENT_TEST_XDIST:-}"
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
  IMAGE_FINGERPRINT="$(docker_test_value_fingerprint "backend-ref:$COMMIT_SHA")"
  IMAGE_TAG="better-agent-backend-tests:ref-${IMAGE_FINGERPRINT}"
  echo "run-backend-tests: materializing $IMAGE_TAG pinned to commit $COMMIT_SHA (ref: $REF)"
  DOCKER_TEST_IMAGE_KIND=ref
  ARCHIVE_PATH="$DOCKER_TEST_RUN_DIR/context"
  docker_test_snapshot_git_ref "$REPO_ROOT" "$COMMIT_SHA" "$ARCHIVE_PATH"
  docker_test_materialize_image "$IMAGE_FINGERPRINT" "$IMAGE_TAG" \
    -f "$ARCHIVE_PATH/docker/Dockerfile.test" --target full -t "$IMAGE_TAG" "$ARCHIVE_PATH"
else
  SNAPSHOT_PATH="$DOCKER_TEST_RUN_DIR/context"
  docker_test_snapshot_context "$REPO_ROOT" "$SNAPSHOT_PATH" \
    "$REPO_ROOT/.dockerignore" \
    "$DOCKERFILE" \
    "$REPO_ROOT/docker/entrypoint-test.sh" \
    "$REPO_ROOT/sdk/runtime-requirements.txt" \
    "$REPO_ROOT/vendor" \
    "$REPO_ROOT/backend/requirements.txt" \
    "$REPO_ROOT/backend/requirements-claude.txt" \
    "$REPO_ROOT/backend/requirements-test.txt"
  IMAGE_FINGERPRINT="$(docker_test_fingerprint "$SNAPSHOT_PATH" \
    "$SNAPSHOT_PATH/.dockerignore" \
    "$SNAPSHOT_PATH/docker/Dockerfile.test" \
    "$SNAPSHOT_PATH/docker/entrypoint-test.sh" \
    "$SNAPSHOT_PATH/sdk/runtime-requirements.txt" \
    "$SNAPSHOT_PATH/vendor" \
    "$SNAPSHOT_PATH/backend/requirements.txt" \
    "$SNAPSHOT_PATH/backend/requirements-claude.txt" \
    "$SNAPSHOT_PATH/backend/requirements-test.txt")"
  IMAGE_TAG="better-agent-backend-tests:deps-${IMAGE_FINGERPRINT}"
  BIND_MOUNT_REPO=1
  echo "run-backend-tests: materializing $IMAGE_TAG (deps only; live working tree is bind-mounted at run time)"
  DOCKER_TEST_IMAGE_KIND=deps
  docker_test_materialize_image "$IMAGE_FINGERPRINT" "$IMAGE_TAG" \
    -f "$SNAPSHOT_PATH/docker/Dockerfile.test" --target deps -t "$IMAGE_TAG" "$SNAPSHOT_PATH"
fi

RUN_ARGS=(--rm)
# BETTER_AGENT_TEST_CPUS / _MEMORY / _CPU_SHARES (see
# docker_test_resource_cap_args in lib/docker-test-lifecycle.sh) — unset
# leaves this identical to the long-standing --cpus=2 default.
while IFS= read -r resource_cap_arg; do
  RUN_ARGS+=("$resource_cap_arg")
done < <(docker_test_resource_cap_args)
if [ -n "${RUN_LLM_TESTS:-}" ]; then
  RUN_ARGS+=(-e "RUN_LLM_TESTS=${RUN_LLM_TESTS}")
fi
# BETTER_AGENT_TEST_CHOWN (see docker_test_chown_env_args in
# lib/docker-test-lifecycle.sh): only emitted on native Linux Docker hosts,
# where entrypoint-test.sh needs it to hand bind-mounted /repo ownership
# back to the invoking user; a no-op on macOS/Docker Desktop.
while IFS= read -r chown_env_arg; do
  RUN_ARGS+=("$chown_env_arg")
done < <(docker_test_chown_env_args)

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

# Per-test timeout: kept in its own array (populated only via +=, never by
# reading PYTEST_ARGS — empty-array reads trip set -u on bash 3.2, see the
# coverage block below) and placed ahead of PYTEST_ARGS at the docker_test_run
# call so an explicit `-- --timeout=...` from the caller still wins (pytest
# keeps the last occurrence of a repeated flag). See the header comment for
# the signal-vs-thread method rationale.
TIMEOUT_PYTEST_ARGS=()
if [ -n "${BETTER_AGENT_TEST_TIMEOUT:-}" ]; then
  TIMEOUT_PYTEST_ARGS+=(
    "--timeout=${BETTER_AGENT_TEST_TIMEOUT}"
    --timeout-method=signal
  )
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
#
# docker_test_xdist_pytest_args (lib/docker-test-lifecycle.sh) is the single
# source of truth for the actual flags (-n <workers> --dist loadfile) — see
# that function's header for why --dist loadfile is mandatory, not optional,
# whenever a worker count is set.
while IFS= read -r xdist_pytest_arg; do
  PYTEST_ARGS+=("$xdist_pytest_arg")
done < <(docker_test_xdist_pytest_args "$PARALLEL_WORKERS")

echo "run-backend-tests: running tests in $IMAGE_TAG"
TEST_STATUS=0
# ${arr[@]+"${arr[@]}"} guards against bash 3.2 (macOS), where reading an
# empty array under `set -u` raises "unbound variable". TIMEOUT_PYTEST_ARGS is
# empty without BETTER_AGENT_TEST_TIMEOUT and PYTEST_ARGS is empty on a bare
# invocation, so both must be conditionally expanded.
docker_test_run "${RUN_ARGS[@]}" "$IMAGE_TAG" ${TIMEOUT_PYTEST_ARGS[@]+"${TIMEOUT_PYTEST_ARGS[@]}"} ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"} || TEST_STATUS=$?
docker_test_cleanup
exit "$TEST_STATUS"
