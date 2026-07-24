# Running Better Agent in Docker

This builds and runs the real app in a container — not the throwaway
installer smoke test at `tests/install-smoke/` (that one only proves
`scripts/install.py` still works from scratch; it never boots a server).

Linux has no `install-macos.sh`/`install-windows.ps1` equivalent (see
`INSTALL.md`). This image is the Linux path: it builds the frontend,
builds a real backend venv, installs a real provider CLI, and serves the
app with `uvicorn`.

## Why credentials work differently here

`backend/auth_secrets.py` normally stores login credentials in the OS
keychain (macOS Keychain, Windows Credential Manager, or — on a real
Linux desktop — whatever Secret Service daemon the `keyring` package
finds). A minimal container has none of that: no D-Bus session, no
`gnome-keyring`/`kwallet`. Rather than faking a keyring daemon inside the
image, this container runs with `BETTER_AGENT_HEADLESS_AUTH=1`, which
makes `auth_secrets.py` read credentials from env vars and mounted secret
files instead. It is off by default everywhere else — a real Linux
desktop keeps using its OS keyring unchanged.

In this mode, credentials are operator-supplied at container start and
cannot be changed from the web UI's account settings; update the env
var / secret files and restart the container instead.

## First run

1. Generate a password hash (never store the plaintext password itself):

   ```bash
   ./scripts/hash-password.py > docker/secrets/password_hash
   ```

2. (Optional but recommended) generate a session secret so logins survive
   a container restart. If you skip this, the container generates one in
   memory on every boot and every existing session is invalidated on
   restart:

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))" > docker/secrets/session_secret
   ```

3. Copy `docker/.env.example` to `docker/.env` and set `BETTER_AGENT_USERNAME`
   (and `BETTER_AGENT_BACKEND_PORT` if you don't want the default `18765`).

4. Build and start:

   ```bash
   docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build
   ```

5. Open `http://localhost:18765` (or your chosen port) and log in with the
   username from step 3 and the plaintext password you hashed in step 1.

## Persistence

All durable state (`BETTER_AGENT_HOME` — sessions, projects, the
installation profile) lives on the `better-agent-data` named volume, not
in the image. Rebuilding the image (`--build`) does not lose it;
`docker compose down -v` does.

## What this image does NOT cover

- **Provider CLI auth.** Installing Claude Code (or another provider CLI)
  gets you the binary; you still need to authenticate it the same way you
  would on a bare host (e.g. `docker compose exec better-agent claude
  /login`, or mount a pre-authenticated config directory as its own
  volume).
- **TLS / reverse proxy.** The container serves plain HTTP on the
  container network; put a reverse proxy in front for TLS and, if you
  want it to see real client IPs, add `--proxy-headers` to
  `docker/entrypoint.sh`'s `uvicorn` invocation — only do this if the
  proxy is trusted, since `backend/auth.py`'s login rate limiting keys off
  the address `uvicorn` reports as the peer.
- **Mobile / desktop-shell integrations.** The image installs with
  `--mode desktop-ui-only` (the "Desktop UI" here means the web
  dashboard, not the native macOS/Windows shell app) — no mobile app
  build tooling, no Better Agent extensions/skills/MCPs. Pass
  `--build-arg BETTER_AGENT_INSTALL_MODE=default` to `docker build` for
  the full integration set.
