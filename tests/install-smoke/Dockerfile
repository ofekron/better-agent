# Better Agent — clean-room install smoke test.
#
# Goal: every push validates that scripts/install.py — the cross-platform
# core both scripts/install-macos.sh and scripts/install-windows.ps1 hand
# off to after their OS-package-manager bootstrap (brew/xcode-select or
# winget) — actually runs end to end on a from-scratch machine with only
# the documented prerequisites present (git, python3, uv; see INSTALL.md
# Part 1.1's prerequisite table).
#
# This is DELIBERATELY narrower than a full macOS/Windows install:
#   - It does not exercise install-macos.sh / install-windows.ps1 or their
#     brew/xcode-select/winget bootstrap steps. Those only run natively on
#     their real OS and are covered by installation-mode-parity.yml's
#     syntax-check steps on macos-14 / windows-2022 runners.
#   - It does not run ./run.sh (submodule init, npm install, frontend
#     build, macOS-keychain-backed auth bootstrap) — run.sh's first-run
#     auth flow is macOS Keychain / Windows Credential Manager specific
#     and has no clean equivalent in a minimal Linux container.
#   - The provider CLI is a stub (see below), so no real provider install
#     or network call to a provider's install script happens.
#
# What IS real: scripts/install.py runs unmodified, resolves a real `uv`,
# builds a real backend venv from requirements.txt (including the vendored
# wheels under vendor/), verifies a provider executable's identity, and
# persists a real installation.json profile via backend/installation_profile.py.
# `docker build` failing IS the test signal — nothing here is `docker run`.

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/root

# git: cloned by end users before ever touching install.py, and some
#   pip installs shell out to it for VCS deps.
# curl + ca-certificates: uv's official installer.
# build-essential + libffi-dev: defensive fallback for argon2-cffi/cffi
#   if no manylinux wheel matches this base image's glibc/arch.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git \
      curl \
      ca-certificates \
      build-essential \
      libffi-dev \
 && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Isolate from any real Better Agent state per backend/paths.py::bc_home()
# — this container's $BETTER_AGENT_HOME must never be a real user's home.
ENV BETTER_AGENT_HOME=/root/.better-agent-install-smoke

# Stub the provider CLI so provider_setup.verified_provider_identity()
# finds an already-installed, already-verifying executable and skips the
# real (network-dependent) provider install path entirely. Mirrors the
# ProviderInstaller for kind="claude" in backend/provider_setup.py:
# command="claude", verify_argv=("claude", "--version").
RUN mkdir -p /usr/local/bin \
 && printf '#!/bin/sh\ncase "$1" in\n  --version) echo "claude-stub 0.0.0-install-smoke"; exit 0 ;;\nesac\nexit 0\n' \
      > /usr/local/bin/claude \
 && chmod +x /usr/local/bin/claude

# .dockerignore strips .git, node_modules, the private nested repo, and
# build/venv artifacts so this stays a clean copy of the working tree.
COPY . /repo
WORKDIR /repo

# One RUN so any failing check fails the image build (and CI).
RUN set -eux; \
    python3 scripts/install.py --mode desktop-ui-only --provider claude --yes; \
    test -f "$BETTER_AGENT_HOME/installation.json" \
      || { echo "installation.json was not written"; exit 1; }; \
    python3 -c "import json,sys; p=json.load(open('$BETTER_AGENT_HOME/installation.json')); \
sys.exit(0 if p.get('status')=='active' and p.get('mode')=='desktop-ui-only' and p.get('provider')=='claude' else 1)" \
      || { echo "installation.json has unexpected content:"; cat "$BETTER_AGENT_HOME/installation.json"; exit 1; }; \
    test -f backend/.active-venv \
      || { echo "backend/.active-venv pointer was not created"; exit 1; }; \
    VENV_DIR="backend/$(cat backend/.active-venv)"; \
    "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, argon2" \
      || { echo "activated venv is missing expected base packages"; exit 1; }; \
    ./scripts/install-bagent.sh; \
    command -v bagent >/dev/null 2>&1 || [ -x "$HOME/.local/bin/bagent" ] \
      || { echo "bagent launcher was not installed"; exit 1; }; \
    echo "OK: install-smoke passed all checks"

# No CMD: this image is built-and-thrown-away. `docker build` returning 0
# is the success signal; CI never `docker run`s it.
