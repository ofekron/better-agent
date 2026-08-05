#!/usr/bin/env bash
# Better Agent container entrypoint.
#
# The venv, provider CLI, and frontend build are baked into the image at
# `docker build` time (see Dockerfile) — that part never re-runs here.
# The persistent Python supervisor acquires the state-home launcher lease
# before it activates a first-boot installation profile or starts uvicorn.

set -euo pipefail

cd /repo/backend
VENV_DIR="$(python3 dependency_plan.py active)"
PY="$VENV_DIR/bin/python"

export BETTER_CLAUDE_BACKEND_PORT="${BETTER_AGENT_BACKEND_PORT:-18765}"
export BETTER_AGENT_BACKEND_PORT="${BETTER_AGENT_BACKEND_PORT:-18765}"
export BETTER_AGENT_BACKEND_BIND_HOST="0.0.0.0"

# better_agent_sdk (repo-root sdk/) is a plain source directory, not a pip
# package — run.sh puts it on PYTHONPATH for every backend invocation
# (alongside the repo root and backend/ itself). desktop/ is intentionally
# left off: it's only imported behind `sys.frozen` checks for the
# PyInstaller-bundled desktop app, which this container never is, and it's
# excluded from the image entirely (.dockerignore) since it's macOS/Windows
# app packaging, not server runtime.
export PYTHONPATH="/repo:/repo/backend:/repo/sdk"

echo "entrypoint: starting uvicorn on 0.0.0.0:${BETTER_AGENT_BACKEND_PORT}"
exec "$PY" -m docker_backend_supervisor
