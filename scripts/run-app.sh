#!/usr/bin/env bash
# Spin up a real, full-stack Better Agent instance (backend + built frontend +
# provider CLI) in Docker, on demand — pinned to any git ref (dev/qa/main, a
# commit, a branch) or to the live working tree, without touching the host's
# running dev stack or its dev/qa/main sibling worktrees.
#
# Builds docker/Dockerfile (the real production image — see docker/README.md
# for what it does and does not cover) and runs it detached, keyed by ref so
# multiple refs can run side by side on different ports with independent
# persisted state (each ref gets its own named Docker volume).
#
# Usage:
#   ./scripts/run-app.sh                        # build+run the live working tree
#   ./scripts/run-app.sh --ref qa                # build+run an exact ref via `git archive`
#   ./scripts/run-app.sh --ref qa --port 28766   # run two refs side by side
#   ./scripts/run-app.sh --ref qa --fresh-data   # drop that ref's persisted /data first
#   ./scripts/run-app.sh --stop                  # stop+remove the working-tree instance
#   ./scripts/run-app.sh --stop --ref qa         # stop+remove the `qa` instance
#
# Every instance is `docker run -d --rm`, so `--stop` (or `docker stop
# <name>`) is a complete teardown of the container — nothing to `docker rm`
# afterward. Persisted app state (sessions, projects, installation profile)
# lives in the per-ref named volume, which survives `--stop` and is only
# ever deleted by `--fresh-data` or `docker volume rm` yourself.
#
# Login credentials: this image only supports BETTER_AGENT_HEADLESS_AUTH
# (see docker/README.md) — no OS keychain inside a container. On first run
# with no docker/secrets/{password_hash,session_secret}, this script
# bootstraps both automatically (a fresh random password, hashed via
# scripts/hash-password.py run inside the backend test deps image — never
# on the host's own possibly-driftedpython, see CLAUDE.md's Docker-only
# backend-Python rule) and prints the plaintext password ONCE. All later
# runs (any ref, any port) share those same credentials, matching
# docker-compose.yml's single-secrets model. Delete docker/secrets/* and
# re-run to rotate.
#
# What this script does NOT do (see docker/README.md's own list): provider
# CLI auth (still `docker exec <container> claude /login` yourself, or
# mount a pre-authenticated config dir), TLS/reverse proxy.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DOCKERFILE="$REPO_ROOT/docker/Dockerfile"
SECRETS_DIR="$REPO_ROOT/docker/secrets"

REF=""
PORT="28765"
FRESH_DATA=0
STOP=0
INSTALL_MODE="desktop-ui-only"
PROVIDER="claude"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --fresh-data)
      FRESH_DATA=1
      shift 1
      ;;
    --stop)
      STOP=1
      shift 1
      ;;
    --install-mode)
      INSTALL_MODE="$2"
      shift 2
      ;;
    --provider)
      PROVIDER="$2"
      shift 2
      ;;
    *)
      echo "run-app: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
run-app: docker is not installed or not on PATH.

This script intentionally does NOT auto-install Docker. Options:
  - macOS:  install Docker Desktop or `brew install --cask orbstack`
  - Linux:  follow https://docs.docker.com/engine/install/

Then re-run: ./scripts/run-app.sh
EOF
  exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
  echo "run-app: $DOCKERFILE not found — wrong checkout?" >&2
  exit 1
fi

# REF_KEY names the container/volume/image for this ref — "workingtree" for
# the no-ref path (matches run-backend-tests.sh's IMAGE_TAG convention),
# otherwise the resolved commit's short sha so re-running the same ref (even
# under a different branch name that still points at it) reuses state.
if [ -n "$REF" ]; then
  COMMIT_SHA="$(git -C "$REPO_ROOT" rev-parse --verify "$REF")"
  REF_KEY="$(git -C "$REPO_ROOT" rev-parse --short "$COMMIT_SHA")"
else
  REF_KEY="workingtree"
fi
CONTAINER_NAME="better-agent-app-${REF_KEY}"
VOLUME_NAME="better-agent-app-data-${REF_KEY}"
IMAGE_TAG="better-agent-app:${REF_KEY}"

if [ "$STOP" = "1" ]; then
  echo "run-app: stopping $CONTAINER_NAME"
  docker stop "$CONTAINER_NAME"
  exit 0
fi

if [ "$FRESH_DATA" = "1" ]; then
  echo "run-app: dropping persisted data volume $VOLUME_NAME"
  docker volume rm "$VOLUME_NAME" 2>/dev/null || true
fi

# ensure_docker_secrets: bootstrap docker/secrets/{password_hash,session_secret}
# on first use. Shared across every ref/port — matches docker-compose.yml's
# single-secrets model (one login for the whole app, not per-ref).
ensure_docker_secrets() {
  mkdir -p "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"

  if [ ! -f "$SECRETS_DIR/session_secret" ]; then
    echo "run-app: generating docker/secrets/session_secret"
    if command -v openssl >/dev/null 2>&1; then
      SECRET_HEX="$(openssl rand -hex 32)"
    else
      SECRET_HEX="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    fi
    # Subshell so the tightened umask doesn't leak into the rest of the script.
    (umask 177; printf '%s' "$SECRET_HEX" > "$SECRETS_DIR/session_secret")
  fi

  if [ ! -f "$SECRETS_DIR/password_hash" ]; then
    echo "run-app: no password_hash yet — bootstrapping a fresh login."

    # Never pass the plaintext via argv/env (shell history, `ps`, container
    # inspect) — write it to a private tempdir file that only this script
    # and the throwaway hashing container can read, exactly the contract
    # scripts/hash-password.py's --password-file documents.
    local pw_dir pw_plain
    pw_dir="$(mktemp -d)"
    trap 'rm -rf "$pw_dir"' RETURN
    if command -v openssl >/dev/null 2>&1; then
      pw_plain="$(openssl rand -base64 24 | tr -d '=+/\n')"
    else
      pw_plain="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    fi
    # mktemp -d already creates the dir 0700; still scope the umask to a
    # subshell so it doesn't leak into the rest of the script either.
    (umask 177; printf '%s' "$pw_plain" > "$pw_dir/pw")

    # Hashing needs argon2-cffi, a backend/requirements.txt dependency — run
    # it inside the same deps image scripts/run-backend-tests.sh builds/uses
    # rather than against the host's own python3 (CLAUDE.md: backend Python
    # only ever runs via Docker, never a host venv, since those drift).
    local deps_image="better-agent-backend-tests:deps"
    echo "run-app: building $deps_image (only if not already cached) to hash the new password"
    docker build \
      -f "$REPO_ROOT/docker/Dockerfile.test" --target deps -t "$deps_image" "$REPO_ROOT" >/dev/null

    # --entrypoint python3 overrides the deps image's ENTRYPOINT (`python -m
    # pytest`, see docker/Dockerfile.test) — without it, this command's args
    # get appended to pytest's argv instead of replacing it.
    docker run --rm \
      --entrypoint python3 \
      -v "$REPO_ROOT:/repo" \
      -v "$pw_dir/pw:/tmp/pw:ro" \
      "$deps_image" \
      /repo/scripts/hash-password.py --password-file /tmp/pw --out /repo/docker/secrets/password_hash

    cat <<EOF

============================================================
run-app: generated a new login — this password is shown ONCE
  username: admin (override with BETTER_AGENT_USERNAME in docker/.env)
  password: ${pw_plain}
Save it now. It is not stored in plaintext anywhere; only its
argon2 hash lives in docker/secrets/password_hash.
============================================================

EOF
  fi
}
ensure_docker_secrets

USERNAME="admin"
if [ -f "$REPO_ROOT/docker/.env" ]; then
  ENV_USERNAME="$(grep -E '^BETTER_AGENT_USERNAME=' "$REPO_ROOT/docker/.env" | tail -1 | cut -d= -f2-)"
  if [ -n "$ENV_USERNAME" ]; then
    USERNAME="$ENV_USERNAME"
  fi
fi

if [ -n "$REF" ]; then
  echo "run-app: building $IMAGE_TAG pinned to commit $COMMIT_SHA (ref: $REF)"
  git -C "$REPO_ROOT" archive "$COMMIT_SHA" \
    | docker build \
        -f docker/Dockerfile \
        --build-arg "BETTER_AGENT_INSTALL_MODE=${INSTALL_MODE}" \
        --build-arg "BETTER_AGENT_PROVIDER=${PROVIDER}" \
        -t "$IMAGE_TAG" -
else
  echo "run-app: building $IMAGE_TAG from the live working tree (uncommitted changes included)"
  docker build \
    -f "$DOCKERFILE" \
    --build-arg "BETTER_AGENT_INSTALL_MODE=${INSTALL_MODE}" \
    --build-arg "BETTER_AGENT_PROVIDER=${PROVIDER}" \
    -t "$IMAGE_TAG" "$REPO_ROOT"
fi

# Replace any previous run of this exact ref before starting a new one —
# `docker run` on an already-used --name would otherwise just fail.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "run-app: starting $CONTAINER_NAME on port $PORT"
docker run -d --rm --name "$CONTAINER_NAME" \
  -p "${PORT}:18765" \
  -e BETTER_AGENT_BACKEND_PORT=18765 \
  -e "BETTER_AGENT_USERNAME=${USERNAME}" \
  -e BETTER_AGENT_PASSWORD_HASH_FILE=/run/secrets/password_hash \
  -e BETTER_AGENT_SESSION_SECRET_FILE=/run/secrets/session_secret \
  -v "$SECRETS_DIR/password_hash:/run/secrets/password_hash:ro" \
  -v "$SECRETS_DIR/session_secret:/run/secrets/session_secret:ro" \
  -v "${VOLUME_NAME}:/data" \
  "$IMAGE_TAG" >/dev/null

# Poll the container's own HEALTHCHECK (see docker/Dockerfile) instead of a
# fixed sleep — event-driven on the real readiness signal, bounded so a
# genuinely broken image doesn't hang the script forever.
echo -n "run-app: waiting for $CONTAINER_NAME to become healthy"
DEADLINE=$((SECONDS + 180))
while true; do
  STATUS="$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "unknown")"
  if [ "$STATUS" = "healthy" ]; then
    echo " — healthy."
    break
  fi
  if [ "$STATUS" = "unhealthy" ] || [ "$SECONDS" -ge "$DEADLINE" ]; then
    echo " — gave up (status: $STATUS). Check: docker logs $CONTAINER_NAME" >&2
    exit 1
  fi
  echo -n "."
  sleep 2
done

cat <<EOF
run-app: $CONTAINER_NAME is up.
  URL:       http://localhost:${PORT}
  username:  ${USERNAME}
  volume:    ${VOLUME_NAME} (persists across restarts of this same ref)
  stop with: ./scripts/run-app.sh --stop $( [ -n "$REF" ] && echo "--ref $REF" )
EOF
