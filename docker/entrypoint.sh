#!/usr/bin/env bash
# Better Agent container entrypoint.
#
# The venv, provider CLI, and frontend build are baked into the image at
# `docker build` time (see Dockerfile) — that part never re-runs here.
# What CAN'T be baked in is the installation profile (installation.json),
# because it lives under $BETTER_AGENT_HOME, which is a volume mount: a
# fresh named volume gets Docker's image-content auto-copy (so a rebuilt
# image's profile carries over), but a fresh bind-mounted host directory
# does not. This entrypoint activates the profile on first boot against
# whichever volume state is actually present, then execs uvicorn.
#
# Deliberately does NOT re-run scripts/install.py on every restart: that
# would mint a new installation_profile "generation" every time, and
# generation is durable identity for provider-executable pinning — only
# a genuinely fresh volume should get a fresh one.

set -euo pipefail

cd /repo/backend
VENV_DIR="$(python3 dependency_plan.py active)"
PY="$VENV_DIR/bin/python"

profile_active() {
  "$PY" -c "
import sys
sys.path.insert(0, '.')
import installation_profile
sys.exit(0 if installation_profile.load().get('status') == 'active' else 1)
"
}

if ! profile_active; then
  echo "entrypoint: no active installation profile in \$BETTER_AGENT_HOME — first boot, activating."
  python3 /repo/scripts/install.py \
    --mode "${BETTER_AGENT_INSTALL_MODE:-desktop-ui-only}" \
    --provider "${BETTER_AGENT_PROVIDER:-claude}" \
    --yes
fi

export BETTER_CLAUDE_BACKEND_PORT="${BETTER_AGENT_BACKEND_PORT:-18765}"
export BETTER_AGENT_BACKEND_PORT="${BETTER_AGENT_BACKEND_PORT:-18765}"

# better_agent_sdk (repo-root sdk/) is a plain source directory, not a pip
# package — run.sh puts it on PYTHONPATH for every backend invocation
# (alongside the repo root and backend/ itself). desktop/ is intentionally
# left off: it's only imported behind `sys.frozen` checks for the
# PyInstaller-bundled desktop app, which this container never is, and it's
# excluded from the image entirely (.dockerignore) since it's macOS/Windows
# app packaging, not server runtime.
export PYTHONPATH="/repo:/repo/backend:/repo/sdk"

echo "entrypoint: starting uvicorn on 0.0.0.0:${BETTER_AGENT_BACKEND_PORT}"
# proxy_headers left at uvicorn's default-off (matches backend/app_entry.py's
# production invocation): auth.py's per-IP rate limiting keys off the real
# peer address, and blindly trusting X-Forwarded-* from an unknown client
# would let it spoof that address. Put a trusted reverse proxy in front and
# override this command with --proxy-headers if you terminate TLS upstream.
exec "$PY" -m uvicorn main:app \
  --host 0.0.0.0 \
  --port "${BETTER_AGENT_BACKEND_PORT}"
