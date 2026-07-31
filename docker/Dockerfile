# syntax=docker/dockerfile:1
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

# Installed to /usr/local/bin, not the installer's ~/.local/bin default:
# this build runs as root ($HOME=/root), but the server later runs as a
# non-root user (see USER betteragent below) whose $HOME can't traverse
# /root at all (mode 750/700) to reach a binary placed there, regardless
# of PATH or the binary's own permission bits.
ENV UV_INSTALL_DIR=/usr/local/bin
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /repo

# .dockerignore strips .git, node_modules, the private nested repo, and
# build/venv artifacts so this stays a clean copy of the working tree.
COPY . /repo

# Real frontend build — not a stub. frontend/package.json's vendor deps
# .dockerignore does not exclude.
#
# BuildKit cache mount: persists npm's package cache across builds of
# different refs/commits (see scripts/run-app.sh's --ref path), so only
# actually-changed dependencies re-download instead of every build starting
# from an empty npm cache. Requires the `# syntax` directive above.
RUN --mount=type=cache,target=/root/.npm \
    cd frontend && npm ci && npm run build

# Real backend venv + real provider CLI install (npm install -g
# @anthropic-ai/claude-code) — no stub. `--mode desktop-ui-only` skips the
# mobile/Android toolchain, which has no place in a headless server image;
# override BETTER_AGENT_INSTALL_MODE at build time (--build-arg) for a
# different mode.
ARG BETTER_AGENT_INSTALL_MODE=desktop-ui-only
ARG BETTER_AGENT_PROVIDER=claude
# Cache mount for uv's package cache — same rationale as the npm mount above:
# install.py's dependency_plan drives `uv`, whose default cache lives under
# $HOME/.cache/uv (this RUN still executes as root, HOME=/root at this point).
RUN --mount=type=cache,target=/root/.cache/uv \
    python3 scripts/install.py \
      --mode "${BETTER_AGENT_INSTALL_MODE}" \
      --provider "${BETTER_AGENT_PROVIDER}" \
      --yes

# Same /root-unreachable-by-non-root reasoning as UV_INSTALL_DIR above:
# install to /usr/local/bin so `docker compose exec better-agent bagent`
# works as the non-root runtime user.
RUN BAGENT_BIN_DIR=/usr/local/bin ./scripts/install-bagent.sh

# Headless credential mode is not optional in this image — see
# backend/auth_secrets.py's HEADLESS CONTAINER MODE section and
# docker/README.md for BETTER_AGENT_USERNAME / *_FILE contract.
ENV BETTER_AGENT_HEADLESS_AUTH=1
ENV BETTER_AGENT_HOME=/data

# Better Agent spawns arbitrary provider-CLI subprocesses and touches the
# filesystem by design — the whole point of the app. Everything above ran
# as root because apt/npm/uv need it; the actual server and every
# subprocess it spawns must not. Fixed UID/GID so a bind-mounted /data on
# the host can be chowned to match (see docker/README.md).
RUN groupadd --gid 10001 betteragent \
 && useradd --uid 10001 --gid betteragent --create-home --shell /usr/sbin/nologin betteragent \
 && mkdir -p /data \
 && chown -R betteragent:betteragent /data \
 && chmod -R a+rX /repo \
 && chown betteragent:betteragent /repo/backend \
 && chown betteragent:betteragent /repo/backend/.active-venv /repo/backend/.dependency-plan.lock \
 && chmod -R a+rX "$(npm root -g)"
# The provider CLI's own npm package (e.g. @anthropic-ai/claude-code)
# installs its scoped package directory as 0700 root-only — found by
# actually running this image as betteragent and getting a real
# PermissionError (via os.access -> shutil.which -> resolve_cli_binary)
# that made scripts/install.py think the already-installed CLI was
# missing and try to re-install it at runtime as non-root, which then
# fails on its own for lack of write access to the same tree. `npm root
# -g` instead of hardcoding the package path so this holds for whichever
# provider BETTER_AGENT_PROVIDER selects.
# The directory chown is deliberately non-recursive (backend/.venvs stays
# root-owned and read-only) EXCEPT for two specific pre-existing files
# entrypoint.sh's idempotent scripts/install.py re-run needs to reopen for
# writing on a fresh volume: ACTIVE_POINTER (backend/.active-venv, whose
# replacement only needs the directory's write bit since it's an atomic
# os.replace — already covered by owning backend/ itself) and
# backend/.dependency-plan.lock, which dependency_plan.activation_lock()
# opens in-place with `open(path, "a+b")` — that needs write permission on
# the FILE's own existing inode, which owning just the parent directory
# does not grant.
ENV HOME=/home/betteragent

VOLUME ["/data"]

EXPOSE 18765

# Liveness only (not readiness during the slow first-boot dependency
# resolve) — `/` is the SPA shell, served unauthenticated so this doesn't
# need credentials. `start-period` covers first-boot's install.py run on a
# fresh volume.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${BETTER_AGENT_BACKEND_PORT:-18765}/" || exit 1

RUN chmod +x docker/entrypoint.sh
USER betteragent
ENTRYPOINT ["docker/entrypoint.sh"]
