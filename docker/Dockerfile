# Better Agent — production container image.
#
# Unlike tests/install-smoke/Dockerfile (a throwaway image whose only job
# is to make `docker build` fail when the installer regresses), this is a
# real, runnable image: it builds the frontend, builds the backend venv,
# installs a real provider CLI (npm install, not a stub), and its
# ENTRYPOINT (docker/entrypoint.sh) serves the actual app.
#
# Build (from the repo root, so vendor/ and .dockerignore apply):
#   docker build -f docker/Dockerfile -t better-agent .
# Or via compose (see docker/docker-compose.yml):
#   docker compose -f docker/docker-compose.yml up -d
#
# Auth: this image always runs with BETTER_AGENT_HEADLESS_AUTH=1 (see
# backend/auth_secrets.py) — there is no OS keychain / Secret Service
# daemon in this container, so credentials come from env vars and a
# mounted secret file instead. See docker/README.md before first run.
#
# Linux is not a first-party base-setup target for scripts/install-macos.sh
# / install-windows.ps1 (see INSTALL.md); this Dockerfile is the Linux
# equivalent, built directly against scripts/install.py rather than one of
# those OS-package-manager wrappers.

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git: shells out from a couple of backend paths (e.g. repo introspection).
# curl + ca-certificates + gnupg: uv installer + NodeSource setup script.
# build-essential + libffi-dev: argon2-cffi's cffi backend if no wheel
#   matches this base image's glibc/arch.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git \
      curl \
      ca-certificates \
      gnupg \
      build-essential \
      libffi-dev \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /repo

# .dockerignore strips .git, node_modules, the private nested repo, and
# build/venv artifacts so this stays a clean copy of the working tree.
COPY . /repo

# Real frontend build — not a stub. frontend/package.json's vendor deps
# (@better-agent/provider-config-sync-*) resolve from vendor/*.tgz, which
# .dockerignore does not exclude.
RUN cd frontend && npm ci && npm run build

# Real backend venv + real provider CLI install (npm install -g
# @anthropic-ai/claude-code) — no stub. `--mode desktop-ui-only` skips the
# mobile/Android toolchain, which has no place in a headless server image;
# override BETTER_AGENT_INSTALL_MODE at build time (--build-arg) for a
# different mode.
ARG BETTER_AGENT_INSTALL_MODE=desktop-ui-only
ARG BETTER_AGENT_PROVIDER=claude
RUN python3 scripts/install.py \
      --mode "${BETTER_AGENT_INSTALL_MODE}" \
      --provider "${BETTER_AGENT_PROVIDER}" \
      --yes

RUN ./scripts/install-bagent.sh

# Headless credential mode is not optional in this image — see
# backend/auth_secrets.py's HEADLESS CONTAINER MODE section and
# docker/README.md for BETTER_AGENT_USERNAME / *_FILE contract.
ENV BETTER_AGENT_HEADLESS_AUTH=1
ENV BETTER_AGENT_HOME=/data
VOLUME ["/data"]

EXPOSE 18765

RUN chmod +x docker/entrypoint.sh
ENTRYPOINT ["docker/entrypoint.sh"]
