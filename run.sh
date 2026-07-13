#!/bin/bash
# Better Agent — prod-mode launcher.
#
# Backend runs WITHOUT uvicorn --reload (no code hot-reload).
# Frontend is served as built static files from the backend port
# (no Vite dev server, no HMR).
#
# To pick up frontend OR backend code changes from a browser, the user
# clicks the "Refresh" button in the UI — it POSTs /api/admin/restart,
# which sets a flag file and SIGTERMs uvicorn. The loop below detects
# the flag, starts the new backend, then rebuilds the frontend while the
# backend serves the previous build. The page reloads after the atomic
# frontend swap completes.
#
# Ctrl+C terminates the loop (uvicorn exits without the flag set).
#
# The restart flag lives at ba_home()/restart_requested — same path the
# backend writes via `paths.ba_home()`.
#
# Auth credentials (username + argon2 hash + session secret) live in
# the macOS login keychain under service "better-agent", with legacy
# "better-claude" entries still read and reset.

set -e
set -o pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BA_HOME="${BETTER_AGENT_HOME:-${BETTER_CLAUDE_HOME:-$HOME/.better-claude}}"
export BETTER_AGENT_HOME="${BETTER_AGENT_HOME:-$BA_HOME}"
export BETTER_CLAUDE_HOME="${BETTER_CLAUDE_HOME:-$BA_HOME}"
FLAG="$BA_HOME/restart_requested"
RESULT="$BA_HOME/refresh_result.json"
BACKEND_LOG="$BA_HOME/backend-run.log"
KC_SVC="better-agent"
KC_LEGACY_SVC="better-claude"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
PY="$DIR/backend/.venv/bin/python"
DEFAULT_BACKEND_PORT=18765
BACKEND_PORT="${BETTER_AGENT_BACKEND_PORT:-${BETTER_CLAUDE_BACKEND_PORT:-$DEFAULT_BACKEND_PORT}}"
FRONTEND_PORT="${BETTER_AGENT_FRONTEND_PORT:-${BETTER_CLAUDE_FRONTEND_PORT:-5173}}"
GRACEFUL_RESTART_TIMEOUT_SECONDS="${BETTER_AGENT_GRACEFUL_RESTART_TIMEOUT_SECONDS:-8}"
if ! [[ "$GRACEFUL_RESTART_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || [ "$GRACEFUL_RESTART_TIMEOUT_SECONDS" -lt 1 ]; then
  GRACEFUL_RESTART_TIMEOUT_SECONDS=8
fi
# The venv is created by `uv`, which does not install `pip` into it. Drive
# dependency installs through `uv pip` against the venv's interpreter.
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

mkdir -p "$BA_HOME"

# Default topology path to $BA_HOME/topology.yaml when unset, so the
# multi-machine `nodes` infrastructure (node_store, /api/nodes,
# RemoteProviderProxy) loads if the file is present. Backend tolerates
# the file being absent — it just logs the load failure and runs in
# single-machine mode. Explicit env var overrides the default.
TOPOLOGY_PATH="${BETTER_AGENT_TOPOLOGY_PATH:-${BETTER_CLAUDE_TOPOLOGY_PATH:-$BA_HOME/topology.yaml}}"
export BETTER_AGENT_TOPOLOGY_PATH="$TOPOLOGY_PATH"
export BETTER_CLAUDE_TOPOLOGY_PATH="$TOPOLOGY_PATH"
# Lets /api/admin/restart reject unsafe self-termination when uvicorn was
# launched directly and no outer process exists to rebuild and respawn it.
export BETTER_AGENT_RUN_SH_SUPERVISOR=1
export BETTER_CLAUDE_RUN_SH_SUPERVISOR=1

# --- Keychain helpers ------------------------------------------------
# All calls shell out to /usr/bin/security so the ACL on every stored
# entry is owned by `security` itself — no GUI permission prompt when
# the backend later reads them. See backend/auth_secrets.py for the
# matching invariant.
kc_has() {
  # Check existence only. Do NOT use `-g`: `-g` attempts to READ the
  # stored password, which on some macOS versions returns non-zero
  # when the binary doesn't have read-ACL access yet (chicken-and-
  # egg on the very first read after add-generic-password). Without
  # `-g` we just probe attributes — always permitted, no GUI prompt.
  /usr/bin/security find-generic-password -s "$KC_SVC" -a "$1" >/dev/null 2>&1 \
    || /usr/bin/security find-generic-password -s "$KC_LEGACY_SVC" -a "$1" >/dev/null 2>&1
}
kc_set() {
  /usr/bin/security add-generic-password -U -s "$KC_SVC" -a "$1" -w "$2"
  /usr/bin/security add-generic-password -U -s "$KC_LEGACY_SVC" -a "$1" -w "$2"
}
kc_del() {
  /usr/bin/security delete-generic-password -s "$KC_SVC" -a "$1" >/dev/null 2>&1 || true
  /usr/bin/security delete-generic-password -s "$KC_LEGACY_SVC" -a "$1" >/dev/null 2>&1 || true
}

# --- --reset-auth ----------------------------------------------------
if [ "${1:-}" = "--reset-auth" ]; then
  echo "This will WIPE the stored Better Agent credentials from the"
  echo "macOS keychain (username, password hash, session secret)."
  read -p "Type 'yes' to confirm: " ans
  if [ "$ans" != "yes" ]; then
    echo "Aborted."
    exit 1
  fi
  kc_del username
  kc_del password_hash
  kc_del session_secret
  rm -f "$BA_HOME/qr_auth_state.json"
  echo "Wiped. Run ./run.sh to bootstrap new credentials."
  exit 0
fi

bootstrap_hint() {
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "Run ./scripts/bootstrap-macos.sh, then run ./run.sh again." >&2
    return 0
  fi
  echo "Install the missing prerequisites listed above, then run ./run.sh again." >&2
}

ensure_base_prereqs() {
  local missing=""
  local cmd=""

  for cmd in git npm node curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing="${missing}${missing:+ }$cmd"
    fi
  done
  if [ ! -x "$UV" ]; then
    missing="${missing}${missing:+ }uv"
  fi

  if [ -z "$missing" ]; then
    return 0
  fi

  echo "Missing required startup tool(s): $missing" >&2
  bootstrap_hint
  exit 1
}

kill_port_listeners() {
  local port="$1"
  local pids=""
  local attempts=0

  if ! command -v lsof >/dev/null 2>&1; then
    echo "Cannot kill listeners on :$port because lsof is not installed." >&2
    return 1
  fi

  stop_known_better_agent_port_users "$port"

  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill -15 2>/dev/null || true
  fi

  while [ "$attempts" -lt 20 ]; do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
    if [ -z "$pids" ]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done

  if [ -n "$pids" ]; then
    echo "Force killing remaining PIDs on :$port..."
    echo "$pids" | xargs kill -9 2>/dev/null || true
  fi
  sleep 0.5
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
  if [ -n "$pids" ]; then
    echo "Port :$port is still occupied by listener PID(s):"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN || true
    return 1
  fi
  return 0
}

port_in_use() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  (echo >"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1
}

resolve_port_conflict() {
  local port="$1"
  local label="$2"
  local answer=""
  local new_port=""

  while true; do
    if ! port_in_use "$port"; then
      echo "$port"
      return 0
    fi
    echo >&2
    if command -v lsof >/dev/null 2>&1; then
      echo "$label port :$port is already in use by:" >&2
      lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
    else
      echo "$label port :$port is already in use; listener details are unavailable because lsof is not installed." >&2
    fi
    echo >&2
    if command -v lsof >/dev/null 2>&1; then
      read -r -p "Kill those process(es), use a different port, or abort? [k/p/a]: " answer >&2
    else
      read -r -p "Use a different port or abort? [p/a]: " answer >&2
    fi
    case "$answer" in
      k|K)
        if ! command -v lsof >/dev/null 2>&1; then
          echo "Kill requires lsof. Choose p or a." >&2
          continue
        fi
        kill_port_listeners "$port" || return 1
        ;;
      p|P)
        read -r -p "New $label port: " new_port >&2
        if ! [[ "$new_port" =~ ^[0-9]+$ ]] || [ "$new_port" -lt 1 ] || [ "$new_port" -gt 65535 ]; then
          echo "Port must be a number between 1 and 65535." >&2
          continue
        fi
        port="$new_port"
        ;;
      a|A)
        return 1
        ;;
      *)
        if command -v lsof >/dev/null 2>&1; then
          echo "Choose k, p, or a." >&2
        else
          echo "Choose p or a." >&2
        fi
        ;;
    esac
  done
}

bootout_launchctl_job() {
  local label="$1"
  local domain="gui/$(id -u)"

  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    echo "Stopping launchctl job $label..."
    launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
  fi
}

kill_matching_processes() {
  local label="$1"
  local pattern="$2"
  local pids=""
  local attempts=0
  local pid=""

  for pid in $(pgrep -f "$pattern" 2>/dev/null || true); do
    if [ "$pid" != "$$" ]; then
      pids="${pids}${pids:+ }$pid"
    fi
  done
  if [ -z "$pids" ]; then
    return 0
  fi

  echo "Stopping previous $label process(es): $pids"
  echo "$pids" | xargs kill -15 2>/dev/null || true

  while [ "$attempts" -lt 20 ]; do
    pids=""
    for pid in $(pgrep -f "$pattern" 2>/dev/null || true); do
      if [ "$pid" != "$$" ]; then
        pids="${pids}${pids:+ }$pid"
      fi
    done
    if [ -z "$pids" ]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done

  echo "Force killing previous $label process(es): $pids"
  echo "$pids" | xargs kill -9 2>/dev/null || true
}

process_is_running() {
  local pid="$1"
  local stat=""

  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  stat="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
  case "$stat" in
    *Z*) return 1 ;;
    *) return 0 ;;
  esac
}

FRONTEND_BUILD_PID=""
BACKEND_PID=""
ZAI_STARTUP_CHECK_PID=""
DAEMON_HOST_PID=""

tracked_child_is_running() {
  local pid="$1"
  local ppid=""

  if [ -z "$pid" ] || ! process_is_running "$pid"; then
    return 1
  fi
  ppid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ' || true)"
  [ "$ppid" = "$$" ]
}

collect_descendants() {
  local pid="$1"
  local child=""

  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    collect_descendants "$child"
    echo "$child"
  done
}

reap_completed_children() {
  if [ -n "$FRONTEND_BUILD_PID" ] && ! tracked_child_is_running "$FRONTEND_BUILD_PID"; then
    FRONTEND_BUILD_PID=""
  fi
  if [ -n "$ZAI_STARTUP_CHECK_PID" ] && ! tracked_child_is_running "$ZAI_STARTUP_CHECK_PID"; then
    ZAI_STARTUP_CHECK_PID=""
  fi
}

stop_child_process() {
  local label="$1"
  local pid="$2"
  local attempts=0
  local pids=""

  if ! tracked_child_is_running "$pid"; then
    return 0
  fi

  echo "Stopping $label (PID $pid)..."
  pids="$(collect_descendants "$pid"; echo "$pid")"
  echo "$pids" | xargs kill -15 2>/dev/null || true
  while [ "$attempts" -lt 20 ]; do
    pids="$(echo "$pids" | while read -r child_pid; do
      if [ -n "$child_pid" ] && process_is_running "$child_pid"; then
        echo "$child_pid"
      fi
    done)"
    if [ -z "$pids" ]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done
  echo "Force killing $label (PID $pid)..."
  echo "$pids" | xargs kill -9 2>/dev/null || true
}

shutdown_children() {
  local signal="${1:-TERM}"
  local exit_code=143

  trap - INT TERM
  if [ "$signal" = "INT" ]; then
    exit_code=130
  fi

  echo
  echo "Stopping Better Agent..."
  reap_completed_children
  stop_child_process "startup checker" "$ZAI_STARTUP_CHECK_PID"
  stop_child_process "frontend build" "$FRONTEND_BUILD_PID"
  stop_child_process "daemon host" "$DAEMON_HOST_PID"
  stop_child_process "backend" "$BACKEND_PID"
  exit "$exit_code"
}

trap 'shutdown_children INT' INT
trap 'shutdown_children TERM' TERM

if [ "${BETTER_AGENT_RUN_SH_TEST_SIGNAL_CLEANUP:-0}" = "1" ]; then
  ((sleep 30 & wait) & wait) &
  BACKEND_PID=$!
  (sleep 30 & wait) &
  FRONTEND_BUILD_PID=$!
  (sleep 30 & wait) >/dev/null 2>&1 &
  ZAI_STARTUP_CHECK_PID=$!
  echo "Signal cleanup test ready: backend=$BACKEND_PID frontend=$FRONTEND_BUILD_PID checker=$ZAI_STARTUP_CHECK_PID"
  while true; do
    sleep 1
  done
fi

kill_backend_lock_holder() {
  local lock_path="$BA_HOME/backend.lock"
  local pid=""
  local cmd=""
  local ppid=""
  local cwd=""
  local foreign_checkout=""
  local attempts=0

  if [ ! -f "$lock_path" ]; then
    return 0
  fi
  pid="$(sed -n 's/^pid=//p' "$lock_path" | head -n 1)"
  if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! process_is_running "$pid"; then
    return 0
  fi

  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1 || true)"
  looks_like_ours=0
  case "$cmd" in
    *"$DIR/backend"*uvicorn*"main:app"*|*"$DIR/backend/app_entry.py"*"--serve"*)
      looks_like_ours=1
      ;;
    *uvicorn*"main:app"*)
      # The launcher runs `(cd "$DIR/backend" && source .venv/bin/activate &&
      # exec uvicorn main:app ...)`, so argv shows a relative
      # `.venv/bin/uvicorn` without the absolute checkout path. Accept it when
      # the process cwd is this checkout's backend dir. Also accept a sibling
      # checkout: backend.lock is keyed on BA_HOME (the shared state home),
      # not on the checkout directory, so a previous backend launched from
      # another worktree can legitimately hold the lock and must be replaced
      # here -- otherwise this relaunch can never win the lock and crashes.
      looks_like_ours=1
      if [ -n "$cwd" ] && [ "$cwd" != "$DIR/backend" ]; then
        foreign_checkout="$cwd"
      fi
      ;;
  esac
  if [ "$looks_like_ours" -ne 1 ]; then
    echo "Backend lock is held by PID $pid, but it does not look like this checkout's backend:"
    echo "$cmd"
    return 0
  fi

  if [ -n "$foreign_checkout" ]; then
    echo "Stopping previous Better Agent backend lock holder from a sibling checkout ($foreign_checkout): $pid"
  else
    echo "Stopping previous Better Agent backend lock holder: $pid"
  fi

  # Escalate TERM -> KILL and VERIFY death each round instead of a single
  # best-effort SIGTERM+SIGKILL fire-and-forget. A lock holder that survived
  # one round previously left the lock held indefinitely: the caller had no
  # idea the kill failed, so it proceeded straight into a doomed backend
  # start (fails the Python-side 15s lock retry) which burns a full,
  # expensive startup-checker AI-agent cycle just to hit the same wall
  # again on the next `run.sh` invocation.
  local round=0
  while [ "$round" -lt 3 ]; do
    round=$((round + 1))
    if [ "$round" -eq 1 ]; then
      kill -15 "$pid" 2>/dev/null || true
    else
      echo "Lock holder $pid still alive after round $((round - 1)); escalating to SIGKILL (round $round)..."
      kill -9 "$pid" 2>/dev/null || true
    fi
    attempts=0
    while [ "$attempts" -lt 20 ]; do
      if ! process_is_running "$pid"; then
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
        if [ -n "$ppid" ] && [ "$ppid" != "1" ]; then
          kill -15 "$ppid" 2>/dev/null || true
        fi
        echo "Lock holder $pid stopped (round $round)."
        return 0
      fi
      attempts=$((attempts + 1))
      sleep 0.25
    done
  done

  echo "FATAL: backend lock holder $pid ($cmd) would not die after repeated SIGTERM/SIGKILL — refusing to start a new backend against a lock we cannot free." >&2
  exit 1
}

stop_known_better_agent_port_users() {
  local port="$1"
  if [ "$port" != "$BACKEND_PORT" ]; then
    return 0
  fi
  bootout_launchctl_job "better-claude"
  bootout_launchctl_job "better-claude-provider-config-sync"
  kill_matching_processes \
    "Better Agent backend wrapper" \
    "cd $DIR/backend && .*uvicorn main:app.*--port $port"
  kill_matching_processes \
    "Better Agent backend" \
    "$DIR/backend.*uvicorn main:app.*--port $port"
}

ensure_base_prereqs

echo "Checking startup ports..."
kill_backend_lock_holder
BACKEND_PORT="$(resolve_port_conflict "$BACKEND_PORT" "backend")"
export BETTER_CLAUDE_BACKEND_PORT="$BACKEND_PORT"
export BETTER_CLAUDE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
export BETTER_AGENT_BACKEND_PORT="$BACKEND_PORT"
export BETTER_AGENT_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
export BA_BACKEND_PORT="$BACKEND_PORT"
FRONTEND_PORT="$(resolve_port_conflict "$FRONTEND_PORT" "frontend")"
export BETTER_CLAUDE_FRONTEND_PORT="$FRONTEND_PORT"
export BETTER_AGENT_FRONTEND_PORT="$FRONTEND_PORT"

ensure_provider_config_sync_submodule() {
  local pcs_dir="$DIR/provider-config-sync"

  if [ ! -f "$DIR/.gitmodules" ]; then
    return 0
  fi
  if [ -f "$pcs_dir/package.json" ] && [ -d "$pcs_dir/packages/provider-config-sync-ui/src" ]; then
    return 0
  fi

  echo "Initializing provider-config-sync submodule..."
  git -C "$DIR" submodule update --init provider-config-sync

  if [ ! -f "$pcs_dir/package.json" ] || [ ! -d "$pcs_dir/packages/provider-config-sync-ui/src" ]; then
    echo "provider-config-sync submodule is still missing after git submodule update." >&2
    exit 1
  fi
}

npm_project_hash() {
  local project_dir="$1"
  shift
  (cd "$project_dir" && node - "$@" <<'NODE'
const { createHash } = require("node:crypto");
const { readFileSync } = require("node:fs");

const outer = createHash("sha256");
for (const path of process.argv.slice(2)) {
  const inner = createHash("sha256").update(readFileSync(path)).digest("hex");
  outer.update(`${inner}  ${path}\n`);
}
process.stdout.write(outer.digest("hex"));
NODE
  )
}

sync_npm_project_deps() {
  local project_dir="$1"
  local label="$2"
  local stamp="$project_dir/node_modules/.better-agent-deps.stamp"
  local current=""
  local stamped=""

  if [ ! -f "$project_dir/package-lock.json" ]; then
    echo "$label package-lock.json is missing; cannot install reproducibly." >&2
    exit 1
  fi

  current="$(npm_project_hash "$project_dir" package.json package-lock.json)"
  if [ -f "$stamp" ]; then
    stamped="$(cat "$stamp" 2>/dev/null || true)"
  fi
  if [ -d "$project_dir/node_modules" ] && [ "$stamped" = "$current" ]; then
    echo "$label npm deps unchanged — skipping install."
    return 0
  fi

  echo "Installing $label npm deps..."
  (cd "$project_dir" && npm ci)
  printf '%s' "$current" > "$stamp"
}

ensure_provider_config_sync_submodule
sync_npm_project_deps "$DIR/provider-config-sync" "provider-config-sync"
sync_npm_project_deps "$DIR/frontend" "frontend"

# --- Sync backend dependencies before anything that imports them ----
# Idempotent; cheap when deps are cached. Required so the argon2 import
# in the keychain-bootstrap block below works on a fresh checkout.
sync_backend_deps() {
  local req="$DIR/backend/requirements.txt"
  local stamp="$DIR/backend/.venv/.requirements.stamp"
  local current=""
  local stamped=""

  # Create the venv on a fresh checkout — `uv pip install` won't make one.
  [ -x "$PY" ] || "$UV" venv "$DIR/backend/.venv"

  current="$("$PY" - "$req" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
)"
  if [ -f "$stamp" ]; then
    stamped="$(cat "$stamp" 2>/dev/null || true)"
  fi
  if [ "$stamped" = "$current" ]; then
    echo "Backend deps unchanged — skipping sync."
    return 0
  fi

  echo "Syncing backend deps..."
  (cd "$DIR/backend" && "$UV" pip install -q --python "$PY" -r requirements.txt)
  printf '%s' "$current" > "$stamp"
}
sync_backend_deps

# --- Install the `bagent` CLI command onto PATH (idempotent) --------
# Other tools (e.g. TestApe locator healing) shell out to `bagent`.
bash "$DIR/scripts/install-bagent.sh" || echo "bagent install failed (non-fatal)"

# --- First-time keychain bootstrap ----------------------------------
# A cleartext password is NEVER echoed (terminal scrollback, tmux/CI logs
# would leak a full-access credential). Three modes, none of which print a
# secret:
#   - BA_PASSWORD set    → use it silently (headless / scripted).
#   - interactive TTY    → prompt the operator to choose one.
#   - no TTY and no env  → mint a random one, do NOT print it; onboard via
#                          the login-screen QR, or `--reset-auth` to set a
#                          known password.
# Override the username via BA_USERNAME (defaults to a random ba-XXXX).
FIRST_RUN_AUTH_BOOTSTRAPPED=0
FIRST_RUN_BROWSER_OPENED=0
if [ "$(uname -s)" = "Darwin" ] && { ! kc_has username || ! kc_has password_hash || ! kc_has session_secret; }; then
  FIRST_RUN_AUTH_BOOTSTRAPPED=1
  echo
  echo "Better Agent — first-time auth setup (credentials live in your OS keychain only)."
  UNAME="${BA_USERNAME:-$("$PY" -c "import secrets; print('ba-'+secrets.token_hex(4))")}"
  PW=""
  if [ -n "${BA_PASSWORD:-}" ]; then
    PW="$BA_PASSWORD"
    echo "Using credentials from BA_USERNAME / BA_PASSWORD."
  elif [ -t 0 ]; then
    read -p "Username [$UNAME]: " _u; [ -n "$_u" ] && UNAME="$_u"
    while true; do
      read -s -p "Password: " PW1; echo
      read -s -p "Confirm:  " PW2; echo
      if [ -z "$PW1" ]; then echo "Empty password — try again."; continue; fi
      if [ "$PW1" != "$PW2" ]; then echo "Mismatch — try again."; continue; fi
      PW="$PW1"; break
    done
  else
    PW="$("$PY" -c "import secrets; print(secrets.token_urlsafe(18))")"
    echo "No TTY and no BA_PASSWORD — set a RANDOM password (not shown)."
    echo "Onboard devices via the QR on the login screen, or run ./run.sh --reset-auth to choose one."
  fi
  # Password reaches python via stdin (NOT argv) so it stays out of `ps`.
  HASH=$(printf '%s' "$PW" | "$PY" -c "import sys, argon2; print(argon2.PasswordHasher().hash(sys.stdin.read()))")
  SECRET=$("$PY" -c "import secrets; print(secrets.token_hex(32))")
  kc_set username "$UNAME"
  kc_set password_hash "$HASH"
  kc_set session_secret "$SECRET"
  unset PW PW1 PW2 _u HASH SECRET
  echo
  echo "Stored for user '$UNAME'. Starting backend..."
  echo
  unset UNAME
fi

rm -f "$FLAG"

build_frontend() {
  local request_id="${1:-}"
  local build_log="$BA_HOME/frontend_build.log"
  local status="failed"

  # `npm run build` (scripts/build-atomic.mjs) builds into a temp dir, swaps
  # it into dist/ atomically, and keeps the previous build's content-hashed
  # assets so live tabs don't lose their lazy chunks mid-rebuild.
  echo "Building frontend..."
  if (cd "${ACTIVE_DIR:-$DIR}/frontend" && npm run build 2>&1 | tee "$build_log"); then
    status="succeeded"
  else
    echo "Frontend build failed — serving previous build"
  fi

  if [ -n "$request_id" ]; then
    "$PY" - "$RESULT" "$request_id" "$status" "$build_log" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

result_path = Path(sys.argv[1])
request_id = sys.argv[2]
status = sys.argv[3]
log_path = Path(sys.argv[4])
error = None
if status == "failed":
    try:
        error = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        error = "Frontend build failed; build log was unavailable."

payload = {
    "request_id": request_id,
    "status": status,
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "error": error,
}
tmp_path = result_path.with_suffix(".tmp")
tmp_path.write_text(json.dumps(payload), encoding="utf-8")
os.replace(tmp_path, result_path)
PY
  fi
}

start_frontend_build() {
  local request_id="${1:-}"
  reap_completed_children
  if tracked_child_is_running "$FRONTEND_BUILD_PID"; then
    if [ -n "$request_id" ]; then
      local previous_pid="$FRONTEND_BUILD_PID"
      (wait "$previous_pid" 2>/dev/null || true; build_frontend "$request_id") &
      FRONTEND_BUILD_PID=$!
    fi
    return 0
  fi
  build_frontend "$request_id" &
  FRONTEND_BUILD_PID=$!
}

app_url() {
  echo "http://127.0.0.1:$BACKEND_PORT/"
}

open_first_run_browser() {
  local url="$1"
  if [ "${BETTER_AGENT_NO_BROWSER:-${BETTER_CLAUDE_NO_BROWSER:-0}}" = "1" ]; then
    return 0
  fi
  if [ "$FIRST_RUN_AUTH_BOOTSTRAPPED" -ne 1 ]; then
    return 0
  fi
  if [ "$FIRST_RUN_BROWSER_OPENED" -eq 1 ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    return 0
  fi

  FIRST_RUN_BROWSER_OPENED=1
  case "$(uname -s)" in
    Darwin)
      open "$url" >/dev/null 2>&1 || true
      ;;
    Linux)
      if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 || true
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*)
      cmd.exe /c start "" "$url" >/dev/null 2>&1 || true
      ;;
  esac
}

start_backend() {
  local bind_host
  bind_host=$("$PY" - "$BA_HOME/user_prefs.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    prefs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    prefs = {}

host = prefs.get("network_bind_address", "127.0.0.1")
if host not in ("127.0.0.1", "0.0.0.0"):
    host = "127.0.0.1"
print(host)
PY
)
  # ACTIVE_DIR / BETTER_AGENT_ACTIVE_CHECKOUT are resolved once per loop
  # iteration by the caller before the frontend build, so the built frontend and
  # the backend always target the same checkout. The pointer is written by the
  # switch-control extension; this launcher honors it and reverts on failed
  # starts.
  echo "Starting backend (no --reload) on $bind_host:$BACKEND_PORT..."
  kill_backend_lock_holder
  BACKEND_PORT="$(resolve_port_conflict "$BACKEND_PORT" "backend")"
  export BETTER_CLAUDE_BACKEND_PORT="$BACKEND_PORT"
  export BETTER_CLAUDE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
  export BETTER_AGENT_BACKEND_PORT="$BACKEND_PORT"
  export BETTER_AGENT_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
  export BA_BACKEND_PORT="$BACKEND_PORT"
  # Capture uvicorn stdout+stderr to a fresh log file AND the terminal so the
  # startup checker can read a crash traceback after the backend exits. `exec`
  # makes BACKEND_PID the uvicorn process itself (clean kill/wait); tee drains
  # on EOF when uvicorn exits.
  : > "$BACKEND_LOG"
  echo "--- backend start $(date '+%Y-%m-%dT%H:%M:%S%z') port=$BACKEND_PORT ---" >> "$BACKEND_LOG"
  (cd "$ACTIVE_DIR/backend" && source .venv/bin/activate && exec uvicorn main:app --host "$bind_host" --port "$BACKEND_PORT" --no-proxy-headers --ws-per-message-deflate false) > >(tee -a "$BACKEND_LOG") 2>&1 &
  BACKEND_PID=$!
}

wait_for_backend() {
  local attempts=0
  local url=""
  while [ "$attempts" -lt 240 ]; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "Backend exited before becoming healthy."
      wait "$BACKEND_PID" || true
      BACKEND_PID=""
      return 1
    fi
    if curl -fsS "http://127.0.0.1:$BACKEND_PORT/healthz" >/dev/null 2>&1; then
      echo "Backend is healthy."
      url="$(app_url)"
      echo "Better Agent is ready: $url"
      open_first_run_browser "$url"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done
  echo "Backend did not become healthy within 60 seconds."
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" || true
  BACKEND_PID=""
  return 1
}

wait_for_backend_exit() {
  local restart_attempts=0
  local restart_limit=$((GRACEFUL_RESTART_TIMEOUT_SECONDS * 4))

  while process_is_running "$BACKEND_PID"; do
    if [ -f "$FLAG" ]; then
      if [ "$restart_attempts" -eq 0 ]; then
        echo "Restart requested — waiting up to ${GRACEFUL_RESTART_TIMEOUT_SECONDS}s for graceful shutdown..."
      fi
      if [ "$restart_attempts" -ge "$restart_limit" ]; then
        echo "Graceful restart timeout expired; forcing backend shutdown."
        kill -9 "$BACKEND_PID" 2>/dev/null || true
        break
      fi
      restart_attempts=$((restart_attempts + 1))
    fi
    sleep 0.25
  done

  wait "$BACKEND_PID" || true
  BACKEND_PID=""
}

run_zai_startup_check() {
  local backend_healthy="${1:-0}"
  local log_path="$BA_HOME/zai_glm52_startup_check.log"

  if [ "${BETTER_AGENT_SKIP_ZAI_STARTUP_CHECK:-${BETTER_CLAUDE_SKIP_ZAI_STARTUP_CHECK:-0}}" = "1" ]; then
    echo "Skipping Z.AI glm-5.2 startup checker because startup checker skip env is set."
    return 0
  fi

  echo "Running Z.AI glm-5.2 startup checker agent (backend_healthy=$backend_healthy)..."
  if "$PY" - "$DIR" "$BACKEND_PORT" "$BACKEND_PID" "$backend_healthy" "$BACKEND_LOG" >"$log_path" 2>&1 <<'PY'
import json
import os
import re
import shutil
import subprocess
import sys
import time

repo = sys.argv[1]
backend_port = sys.argv[2]
backend_pid = sys.argv[3]
backend_healthy = sys.argv[4] == "1"
backend_log = sys.argv[5]
checker_started_epoch = time.time()
sys.path.insert(0, os.path.join(repo, "backend"))

import config_store

provider = None
for candidate in config_store.list_providers().get("providers", []):
    if str(candidate.get("name") or "").casefold() == "z.ai":
        provider = config_store.get_provider_with_key(str(candidate.get("id") or ""))
        break
if not provider:
    print("Z.AI provider is not configured; skipping optional startup checker.")
    raise SystemExit(0)
if provider.get("kind") != "claude":
    raise SystemExit("Z.AI startup check requires a Claude-compatible provider")
if provider.get("mode") != "api_key":
    raise SystemExit("Z.AI startup check requires API-key mode")
api_key = str(provider.get("api_key") or "")
if not api_key:
    raise SystemExit("Z.AI provider API key is missing")

claude = shutil.which("claude")
if not claude:
    raise SystemExit("claude CLI is not on PATH")

env = os.environ.copy()
env["ANTHROPIC_API_KEY"] = api_key
base_url = str(provider.get("base_url") or "")
if base_url:
    env["ANTHROPIC_BASE_URL"] = base_url
else:
    env.pop("ANTHROPIC_BASE_URL", None)
env.pop("ANTHROPIC_AUTH_TOKEN", None)
env["BETTER_AGENT_SKIP_ZAI_STARTUP_CHECK"] = "1"
env["BETTER_CLAUDE_SKIP_ZAI_STARTUP_CHECK"] = "1"
config_dir = str(provider.get("config_dir") or "")
if config_dir:
    env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(os.path.expandvars(config_dir))
else:
    env.pop("CLAUDE_CONFIG_DIR", None)

prompt = f"""
You are the Better Agent run.sh startup checker. You are running as a direct
Claude Code CLI process configured for the Z.AI Claude-compatible provider,
not through Better Agent.

Goal: verify that the run.sh invocation that spawned you actually succeeded.
Do not treat a successful model response as success. Success means the Better
Agent app is operational.

Current launch:
- repo: {repo}
- backend port: {backend_port}
- backend pid: {backend_pid}
- backend reached healthy: {"yes" if backend_healthy else "NO — backend exited before becoming healthy"}
- backend run log (uvicorn stdout+stderr, includes any startup traceback): {backend_log}
- state home: {os.environ.get("BETTER_AGENT_HOME") or os.environ.get("BETTER_CLAUDE_HOME") or ""}
- checker log: {os.path.join(os.environ.get("BETTER_AGENT_HOME") or os.environ.get("BETTER_CLAUDE_HOME") or os.path.expanduser("~/.better-claude"), "zai_glm52_startup_check.log")}
- checker started epoch: {checker_started_epoch}

If "backend reached healthy" is NO, the backend crashed on startup. Read the
backend run log above FIRST — it holds the uvicorn traceback (e.g.
ModuleNotFoundError, schema/migration errors, import failures). Diagnose the
root cause from that traceback, fix it in the repo, then rerun run.sh yourself
to confirm the backend comes up. Do NOT report ok while the backend is down.

Required checks:
1. Verify backend health and key REST endpoints using direct shell commands,
   not Better Agent's CLI. Protected /api endpoints may return 401 without a
   browser session; treat 401/403 from protected endpoints as proof that the
   auth gate is alive, not as a startup failure. Public /healthz or /health
   must return 200.
2. Verify the frontend is being served from the backend and the built assets
   are reachable.
3. Inspect recent backend/run logs for ERROR tracebacks, ModuleNotFoundError,
   NameError, AttributeError, frontend build failures, unhandled RuntimeWarning,
   and lag-watchdog/event-loop-blocked lines after this launch began. Ignore
   entries older than the checker started epoch above.
4. If you find a real issue and the fix is clear, edit the repo, run focused
   tests, then rerun run.sh yourself. Your environment already includes
   BETTER_AGENT_SKIP_ZAI_STARTUP_CHECK=1, so reruns will not recursively launch
   another checker. Stop the rerun once you have verified startup.
5. If you cannot prove success, fail clearly.

Rules:
- Do not call backend/cli.py, bagent, or any Better Agent prompt/session path.
- Use shell commands, file inspection, curl, and direct logs.
- Do not stage, commit, push, or stash.
- Keep edits tightly scoped to startup/runtime breakages you can prove.
- Final response must be exactly JSON:
  {{"status":"ok"|"failed","summary":"short reason","fixed":true|false}}
""".strip()

cmd = [
    claude,
    "--bare",
    "-p",
    "--output-format", "json",
    "--permission-mode", "bypassPermissions",
    "--input-format", "text",
    "--model", "glm-5.2",
    prompt,
]
result = subprocess.run(
    cmd,
    cwd=repo,
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    timeout=1800,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
if result.returncode != 0:
    raise SystemExit(result.returncode)
try:
    payload = json.loads(result.stdout or "{}")
except json.JSONDecodeError as exc:
    raise SystemExit(f"claude returned non-JSON output: {exc}") from exc
if isinstance(payload, dict) and payload.get("is_error"):
    raise SystemExit("claude returned is_error=true")
raw_result = payload.get("result") if isinstance(payload, dict) else None
if not isinstance(raw_result, str):
    raise SystemExit("claude JSON output did not contain a string result")

def parse_checker_result(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fenced = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = fenced or re.findall(r"\{[\s\S]*?\}", raw)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
    raise ValueError("no checker status JSON object found")

try:
    checker_result = parse_checker_result(raw_result)
except ValueError as exc:
    raise SystemExit(f"checker result was not JSON: {raw_result}") from exc
if not isinstance(checker_result, dict) or checker_result.get("status") != "ok":
    raise SystemExit(f"startup checker failed: {raw_result}")
PY
  then
    echo "Z.AI glm-5.2 startup checker completed successfully."
    return 0
  fi

  echo "Z.AI glm-5.2 startup checker failed. See $log_path"
  return 0
}

PENDING_REFRESH_ID=""
INITIAL_FRONTEND_BUILD_STARTED=0
ZAI_STARTUP_CHECK_DONE=0
# Platform daemon host: supervises supervisor-lifecycle extension daemons so
# they outlive backend restarts. Runs the launcher checkout's code (fixed
# point) and reads its desired set from ba_home()/daemons/registry.json.
PYTHONPATH="$DIR" "$PY" -m daemonhost &
DAEMON_HOST_PID=$!
# shellcheck source=scripts/switch_launch.sh
source "$DIR/scripts/switch_launch.sh"
if [ "${BETTER_AGENT_RUN_SH_TEST_NORMAL_EXIT_CLEANUP:-0}" = "1" ]; then
  (sleep 30 & wait) &
  FRONTEND_BUILD_PID=$!
  ZAI_STARTUP_CHECK_PID="$("$PY" - <<'PY'
import subprocess

proc = subprocess.Popen(
    ["sleep", "30"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
print(proc.pid)
PY
)"
  echo "Normal exit cleanup test ready: frontend=$FRONTEND_BUILD_PID daemon=$DAEMON_HOST_PID checker=$ZAI_STARTUP_CHECK_PID"
else
  while true; do
    # Line switching: run the backend from the active checkout (dev/main worktree)
    # named by the pointer. Resolve it every iteration — a switch or an auto-revert
    # changes it between restarts, and the frontend build below must target the
    # same checkout the backend will import.
    ACTIVE_DIR="$(resolve_active_checkout "$PY" "$DIR" "$DIR")"
    SWITCH_REQUEST_ID="$(PYTHONPATH="$DIR" "$PY" -m daemonhost.pointer request-id 2>/dev/null || true)"
    export BETTER_AGENT_ACTIVE_CHECKOUT="$ACTIVE_DIR"
    if [ "$ACTIVE_DIR" != "$DIR" ]; then
      echo "Active checkout: $ACTIVE_DIR"
    fi

    # The backend imports the active checkout's built frontend at import time.
    # When no dist exists yet, how it gets built depends on why:
    #  - A line switch to a never-built checkout must build synchronously first,
    #    because the build result decides whether to revert the switch.
    #  - A cold clone (no switch in flight) uses serve-then-build: the backend
    #    starts against mount_frontend()'s placeholder, the frontend builds in
    #    the background, and the backend self-restarts once the build lands.
    # A warm checkout keeps the fast "serve the previous build while rebuilding
    # in the background" path.
    if active_frontend_needs_build "$ACTIVE_DIR"; then
      if PYTHONPATH="$DIR" "$PY" -m daemonhost.pointer is-switching 2>/dev/null; then
        build_frontend "$PENDING_REFRESH_ID"
        PENDING_REFRESH_ID=""
        # If the synchronous build produced no dist (the target checkout's own
        # build failed), starting the backend would crash in mount_frontend().
        # Revert the in-flight switch rather than crash-looping on its target.
        if active_frontend_needs_build "$ACTIVE_DIR" \
          && PYTHONPATH="$DIR" "$PY" -m daemonhost.pointer revert-if-switching \
            --reason "frontend build produced no dist for the switch target" \
            --request-id "$SWITCH_REQUEST_ID" 2>/dev/null; then
          echo "Line switch failed — target frontend did not build — recovering previous checkout..."
          continue
        fi
      else
        start_frontend_build "$PENDING_REFRESH_ID"
        PENDING_REFRESH_ID=""
      fi
    elif [ "$INITIAL_FRONTEND_BUILD_STARTED" -eq 0 ]; then
      start_frontend_build ""
    fi
    INITIAL_FRONTEND_BUILD_STARTED=1

    start_backend
    # Capture health without aborting under `set -e`: a crashed backend must still
    # reach the startup checker so it can read the traceback and auto-fix.
    if wait_for_backend; then
      BACKEND_HEALTHY=1
    else
      BACKEND_HEALTHY=0
    fi

    if [ "$ZAI_STARTUP_CHECK_DONE" -eq 0 ]; then
      # Background the checker: its CLI agent can run up to 30 min, and it must
      # never block the refresh/restart loop or backend serving. It self-contains
      # its own success/failure (always returns 0), so it cannot fail run.sh.
      # A backgrounded child survives run.sh exiting, so on a crash it can still
      # fix + rerun after we break below.
      run_zai_startup_check "$BACKEND_HEALTHY" &
      ZAI_STARTUP_CHECK_PID=$!
      disown "$ZAI_STARTUP_CHECK_PID" 2>/dev/null || true
      ZAI_STARTUP_CHECK_DONE=1
    fi

    if [ "$BACKEND_HEALTHY" -ne 1 ]; then
      # Auto-revert a failed line switch: only fires when a switch is in
      # flight, so an ordinary crash never flips checkouts.
      if PYTHONPATH="$DIR" "$PY" -m daemonhost.pointer revert-if-switching \
        --reason "backend failed to become healthy" --request-id "$SWITCH_REQUEST_ID" 2>/dev/null; then
        echo "Line switch failed — recovering to a runnable checkout..."
        continue
      fi
      echo "Backend never became healthy — startup checker launched in background to fix+rerun; exiting."
      break
    fi
    PYTHONPATH="$DIR" "$PY" -m daemonhost.pointer confirm-healthy \
      --running-dir "$ACTIVE_DIR" --request-id "$SWITCH_REQUEST_ID" 2>/dev/null || true

    if [ -n "$PENDING_REFRESH_ID" ]; then
      start_frontend_build "$PENDING_REFRESH_ID"
      PENDING_REFRESH_ID=""
    fi

    # Block until uvicorn exits. Restart-requested exits are bounded so a stuck
    # shutdown does not leave the UI waiting forever.
    wait_for_backend_exit

    if [ -f "$FLAG" ]; then
      PENDING_REFRESH_ID="$(cat "$FLAG")"
      rm -f "$FLAG"
      echo "Restart requested — restarting backend..."
      continue
    fi

    echo "Backend exited (no restart flag) — stopping."
    break
  done
fi

reap_completed_children
stop_child_process "frontend build" "$FRONTEND_BUILD_PID"
stop_child_process "daemon host" "$DAEMON_HOST_PID"
