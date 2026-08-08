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
#   ./scripts/run-backend-tests.sh --parallel 1            # ESCAPE HATCH: force old single-process behavior
#
# Parallelism default: LOCAL runs now default to pytest-xdist with 3 workers
# (BETTER_AGENT_TEST_XDIST=3 equivalent), matching this box's default
# BETTER_AGENT_TEST_CPUS=3 container cap one worker per whole CPU, and
# matching the shape CI already uses per self-hosted runner (see
# .github/workflows/backend-tests-selfhosted.yml). This used to be opt-in
# only (CI-only) because every knob was deliberately left off for local
# invocations; that meant local runs were single-process (using ~1 of this
# Mac's 8 cores) with no bound on a hung test. 3 was picked, not `auto`
# (nproc, i.e. 8 here), because this is the user's primary interactive
# machine and local agent test runs happen concurrently with everything
# else on it, often several agent sessions at once — defaulting to "use
# every core" would let one test run monopolize the box. 3 workers leaves
# headroom for at least one more concurrent run plus interactive use before
# the box is fully committed.
#
# `--dist loadfile` is applied whenever a worker count is set (see
# docker_test_xdist_pytest_args in lib/docker-test-lifecycle.sh) — it keeps
# every test in one file on the SAME worker PROCESS. File-level isolation is
# the property backend/scripts integration tests actually rely on (temp
# homes engaged per module, fixed ports bound with port=0 for ephemeral
# assignment), and splitting work across worker processes cannot weaken
# that: two files on two different workers share no process state at all,
# which is strictly SAFER than a single process where every file already
# shares one process's module table for the whole run. What stays unvetted
# is splitting a single file's own tests across workers (loadscope/load) —
# never used here.
#
# ESCAPE HATCH (old single-process behavior — needed for pdb, `-s`, or an
# ordering repro that genuinely requires one process): any of
#   - BETTER_AGENT_TEST_XDIST=0
#   - --parallel 1  (or BETTER_AGENT_TEST_XDIST=1)
#   - `-- -p no:xdist` (or any user pytest arg containing `no:xdist`)
# fully disables xdist for that run — no `-n`, no `--dist` flag at all, so
# behavior is identical to a plain `pytest` invocation. An explicit
# `--parallel <N>` (any N) on the command line always wins over
# BETTER_AGENT_TEST_XDIST. `-- -p no:xdist` wins over both, since otherwise
# an auto-appended `-n <workers>` would be an unrecognized argument once the
# xdist plugin itself is disabled.
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
# in lib/docker-test-lifecycle.sh): BETTER_AGENT_TEST_CPUS (default 3 — see
# the parallelism-default rationale above; was 2), BETTER_AGENT_TEST_MEMORY
# (+ matching --memory-swap; default unset/uncapped, same as always),
# BETTER_AGENT_TEST_CPU_SHARES (default unset — docker's normal 1024
# weight). CPUS is now a real local default (not a no-op) to match the new
# xdist worker default 1:1; MEMORY/CPU_SHARES stay opt-in. CI overrides
# CPUS/XDIST per self-hosted runner in
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
# an explicit `-- --timeout=...` still wins. Defaults to 120s locally now
# (same value CI has always used via
# .github/workflows/backend-tests-selfhosted.yml's job env) — a hung test
# used to cost a local session its entire remaining runtime with zero
# diagnostic; now it costs at most ~120s and a stack dump. ESCAPE HATCH:
# BETTER_AGENT_TEST_TIMEOUT=0 disables the timeout entirely (no flag
# emitted), for the rare case a single test is known to legitimately run
# long. signal over thread: the test image is always Linux, and
# pytest-timeout's thread method can't safely interrupt a hung test at all
# (its own docs: on timeout it dumps thread stacks and hard-kills the whole
# pytest process, losing every remaining test in the run) — signal fails
# just the one hung test via SIGALRM and lets the suite continue, which is
# what we want. This was the exact gap in the 2026-08-07 incident: a single
# hung test burned the full ~100-minute runner budget with zero diagnostic
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
# BETTER_AGENT_TEST_XDIST is --parallel's default (matching the
# BETTER_AGENT_TEST_CPUS/_MEMORY/_TIMEOUT pattern for per-runner knobs); an
# explicit --parallel below always overrides it. Unset now defaults to "3"
# (see the parallelism-default rationale in the header comment), not empty
# — "0" or "1" (from either source) are normalized to "" (xdist fully
# disabled) after arg parsing below, which is the documented escape hatch.
PARALLEL_WORKERS="${BETTER_AGENT_TEST_XDIST:-3}"
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

# Escape hatches (BETTER_AGENT_TEST_XDIST=0/1, --parallel 1, or a
# `-- -p no:xdist`-style user pytest arg) all resolve to "" here — see
# docker_test_normalize_xdist_workers's own header and the escape-hatch list
# in this script's header comment.
PARALLEL_WORKERS="$(docker_test_normalize_xdist_workers "$PARALLEL_WORKERS" ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"})"

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
# leaves this at the --cpus=3 local default (see the parallelism-default
# rationale in this script's header comment).
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
while IFS= read -r timeout_pytest_arg; do
  TIMEOUT_PYTEST_ARGS+=("$timeout_pytest_arg")
done < <(docker_test_timeout_pytest_args "${BETTER_AGENT_TEST_TIMEOUT:-120}")

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
# only empty when BETTER_AGENT_TEST_TIMEOUT=0 (the escape hatch) and
# PYTEST_ARGS is empty on a bare invocation, so both must be conditionally
# expanded.
docker_test_run "${RUN_ARGS[@]}" "$IMAGE_TAG" ${TIMEOUT_PYTEST_ARGS[@]+"${TIMEOUT_PYTEST_ARGS[@]}"} ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"} || TEST_STATUS=$?
docker_test_cleanup
exit "$TEST_STATUS"
