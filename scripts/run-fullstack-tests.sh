#!/usr/bin/env bash
# Canonical way to run frontend/tests/fullstack (Playwright) full-stack
# tests in Docker — for humans AND agents.
#
# Runs inside docker/Dockerfile.fullstack-test, never against a host
# backend venv / npm install directly, for the same reproducibility reason
# as scripts/run-backend-tests.sh (see that file's header). Each Playwright
# spec spawns its own real backend subprocess on its own port/home
# (frontend/tests/fullstack/harness/backend.ts) — some specs (e.g.
# chat-turn.spec.ts, worker-delegation.spec.ts) drive real provider CLI
# turns. This is NOT gated by RUN_LLM_TESTS — frontend/tests/fullstack was
# never gated by it natively either, and this script reproduces that
# existing behavior rather than inventing a new gate. Per repo policy, get
# the user's explicit per-run permission before invoking this script if it
# will exercise LLM-spending specs.
#
# Usage:
#   ./scripts/run-fullstack-tests.sh                       # test the working tree
#   ./scripts/run-fullstack-tests.sh --ref <git-ref>        # test an exact commit/tag/branch
#   ./scripts/run-fullstack-tests.sh -- auth-login.spec.ts  # pass args through to `playwright test`
#   ./scripts/run-fullstack-tests.sh -- -g "some test name"
#
# Provider CLI auth: forwarded from the calling shell, never baked into the
# image. Set ANTHROPIC_API_KEY in your shell before invoking this script,
# and/or have a real `claude` login at ~/.claude on the host — this script
# bind-mounts it read-only into the container so `install.py --adopt`
# (frontend/tests/fullstack/harness/backend.ts) has something to adopt.
#
# Playwright artifacts (test-results/, playwright-report/) are written to a
# host-mounted dir, since the container runs --rm and would otherwise
# discard them.

set -euo pipefail

export DOCKER_BUILDKIT=1

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$REPO_ROOT/docker/Dockerfile.fullstack-test"
# shellcheck source=lib/docker-test-lifecycle.sh
source "$HERE/lib/docker-test-lifecycle.sh"

REF=""
PLAYWRIGHT_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF="$2"
      shift 2
      ;;
    --)
      shift
      PLAYWRIGHT_ARGS=("$@")
      break
      ;;
    *)
      echo "run-fullstack-tests: unknown argument: $1 (playwright args must follow --)" >&2
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
run-fullstack-tests: docker is not installed or not on PATH.

This script intentionally does NOT auto-install Docker. Options:
  - macOS:  install Docker Desktop or `brew install --cask orbstack`
  - Linux:  follow https://docs.docker.com/engine/install/

Then re-run: ./scripts/run-fullstack-tests.sh
EOF
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "run-fullstack-tests: $DOCKERFILE not found — wrong checkout?" >&2
  exit 1
fi

docker_test_lifecycle_init fullstack
docker_test_reap_orphans
docker_test_cleanup

BIND_MOUNT_REPO=0
if [ -n "$REF" ]; then
  COMMIT_SHA="$(git -C "$REPO_ROOT" rev-parse --verify "$REF")"
  IMAGE_TAG="better-agent-fullstack-tests:${COMMIT_SHA}"
  echo "run-fullstack-tests: building $IMAGE_TAG pinned to commit $COMMIT_SHA (ref: $REF)"
  DOCKER_TEST_IMAGE_KIND=ref
  git -C "$REPO_ROOT" archive "$COMMIT_SHA" \
    | docker_test_build -f docker/Dockerfile.fullstack-test --target full -t "$IMAGE_TAG" -
else
  IMAGE_TAG="better-agent-fullstack-tests:deps"
  BIND_MOUNT_REPO=1
  echo "run-fullstack-tests: building $IMAGE_TAG (deps only; live working tree is bind-mounted at run time)"
  DOCKER_TEST_IMAGE_KIND=deps
  docker_test_build -f "$DOCKERFILE" --target deps -t "$IMAGE_TAG" "$REPO_ROOT"
fi

RUN_ARGS=(--rm)

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  RUN_ARGS+=(-e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}")
fi
if [ -d "$HOME/.claude" ]; then
  RUN_ARGS+=(-v "$HOME/.claude:/root/.claude:ro")
fi

if [ "$BIND_MOUNT_REPO" = "1" ]; then
  # Working-tree path: bind-mount the live repo READ-ONLY at /repo-src.
  # docker/fullstack-test-entrypoint.sh snapshots it into a container-
  # private /repo before running — dependency_plan.py's venv-activation
  # writes backend/.active-venv into the tree it runs from, and doing that
  # against the host's actual checkout would overwrite the host's marker
  # with a Linux-only venv path, breaking native `run.sh` outside this
  # container. Read-only here is belt-and-suspenders: the entrypoint
  # already never writes back to /repo-src.
  RUN_ARGS+=(-v "$REPO_ROOT:/repo-src:ro")
  # Named volume (not bind mount): the deps image has no baked-in backend/
  # source (see Dockerfile.fullstack-test), so `dependency_plan.py activate`
  # runs lazily on the first spec's backend spawn instead of at build time.
  # This persists uv's resolved venv + package cache across `docker run`
  # invocations (the container-private /repo snapshot itself does not
  # persist) so resolve is fast after the first run.
  RUN_ARGS+=(-v "better-agent-fullstack-uv-cache:/root/.cache/uv")
fi

ARTIFACTS_DIR="$REPO_ROOT/frontend/test-results"
REPORT_DIR="$REPO_ROOT/frontend/playwright-report"
mkdir -p "$ARTIFACTS_DIR" "$REPORT_DIR"
RUN_ARGS+=(-v "$ARTIFACTS_DIR:/repo/frontend/test-results")
RUN_ARGS+=(-v "$REPORT_DIR:/repo/frontend/playwright-report")

echo "run-fullstack-tests: running tests in $IMAGE_TAG"
TEST_STATUS=0
docker_test_run "${RUN_ARGS[@]}" "$IMAGE_TAG" "${PLAYWRIGHT_ARGS[@]}" || TEST_STATUS=$?
docker_test_cleanup
exit "$TEST_STATUS"
