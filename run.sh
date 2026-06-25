#!/bin/bash
# Better Agent — prod-mode launcher.
#
# Backend runs WITHOUT uvicorn --reload (no code hot-reload).
# Frontend is served as built static files from backend port 8000
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
BACKEND_PORT="${BETTER_AGENT_BACKEND_PORT:-${BETTER_CLAUDE_BACKEND_PORT:-8000}}"
FRONTEND_PORT="${BETTER_AGENT_FRONTEND_PORT:-${BETTER_CLAUDE_FRONTEND_PORT:-5173}}"
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

kill_port_listeners() {
  local port="$1"
  local pids=""
  local attempts=0

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

resolve_port_conflict() {
  local port="$1"
  local label="$2"
  local answer=""
  local new_port=""

  while true; do
    if ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "$port"
      return 0
    fi
    echo >&2
    echo "$label port :$port is already in use by:" >&2
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
    echo >&2
    read -r -p "Kill those process(es), use a different port, or abort? [k/p/a]: " answer >&2
    case "$answer" in
      k|K)
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
        echo "Choose k, p, or a." >&2
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

kill_backend_lock_holder() {
  local lock_path="$BA_HOME/backend.lock"
  local pid=""
  local cmd=""
  local ppid=""
  local attempts=0

  if [ ! -f "$lock_path" ]; then
    return 0
  fi
  pid="$(sed -n 's/^pid=//p' "$lock_path" | head -n 1)"
  if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! process_is_running "$pid"; then
    return 0
  fi

  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$cmd" in
    *"$DIR/backend"*uvicorn*"main:app"*|*"$DIR/backend/app_entry.py"*"--serve"*)
      ;;
    *)
      echo "Backend lock is held by PID $pid, but it does not look like this checkout's backend:"
      echo "$cmd"
      return 0
      ;;
  esac

  echo "Stopping previous Better Agent backend lock holder: $pid"
  kill -15 "$pid" 2>/dev/null || true

  while [ "$attempts" -lt 20 ]; do
    if ! process_is_running "$pid"; then
      ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
      if [ -n "$ppid" ] && [ "$ppid" != "1" ]; then
        kill -15 "$ppid" 2>/dev/null || true
      fi
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done

  kill -9 "$pid" 2>/dev/null || true
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

echo "Checking startup ports..."
kill_backend_lock_holder
BACKEND_PORT="$(resolve_port_conflict "$BACKEND_PORT" "backend")"
export BETTER_CLAUDE_BACKEND_PORT="$BACKEND_PORT"
export BETTER_CLAUDE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
export BETTER_AGENT_BACKEND_PORT="$BACKEND_PORT"
export BETTER_AGENT_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
FRONTEND_PORT="$(resolve_port_conflict "$FRONTEND_PORT" "frontend")"
export BETTER_CLAUDE_FRONTEND_PORT="$FRONTEND_PORT"
export BETTER_AGENT_FRONTEND_PORT="$FRONTEND_PORT"

# --- Sync backend dependencies before anything that imports them ----
# Idempotent; cheap when deps are cached. Required so the argon2 import
# in the keychain-bootstrap block below works on a fresh checkout.
echo "Syncing backend deps..."
# Create the venv on a fresh checkout — `uv pip install` won't make one.
[ -x "$PY" ] || "$UV" venv "$DIR/backend/.venv"
"$UV" pip install -q --python "$PY" -r "$DIR/backend/requirements.txt"

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
if ! kc_has username || ! kc_has password_hash || ! kc_has session_secret; then
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
  local tmp_dist="$DIR/frontend/.dist-refresh-$$"
  local old_dist="$DIR/frontend/.dist-previous-$$"
  local build_log="$BA_HOME/frontend_build.log"
  local status="failed"

  rm -rf "$tmp_dist" "$old_dist"
  echo "Building frontend..."
  if (cd "$DIR/frontend" && VITE_OUT_DIR="$tmp_dist" npm run build 2>&1 | tee "$build_log"); then
    if [ -d "$DIR/frontend/dist" ]; then
      mv "$DIR/frontend/dist" "$old_dist"
    fi
    mv "$tmp_dist" "$DIR/frontend/dist"
    rm -rf "$old_dist"
    status="succeeded"
  else
    rm -rf "$tmp_dist"
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
  echo "Starting backend (no --reload) on $bind_host:$BACKEND_PORT..."
  kill_backend_lock_holder
  BACKEND_PORT="$(resolve_port_conflict "$BACKEND_PORT" "backend")"
  export BETTER_CLAUDE_BACKEND_PORT="$BACKEND_PORT"
  export BETTER_CLAUDE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
  export BETTER_AGENT_BACKEND_PORT="$BACKEND_PORT"
  export BETTER_AGENT_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
  # Capture uvicorn stdout+stderr to a fresh log file AND the terminal so the
  # startup checker can read a crash traceback after the backend exits. `exec`
  # makes BACKEND_PID the uvicorn process itself (clean kill/wait); tee drains
  # on EOF when uvicorn exits.
  : > "$BACKEND_LOG"
  echo "--- backend start $(date '+%Y-%m-%dT%H:%M:%S%z') port=$BACKEND_PORT ---" >> "$BACKEND_LOG"
  (cd "$DIR/backend" && source .venv/bin/activate && exec uvicorn main:app --host "$bind_host" --port "$BACKEND_PORT") > >(tee -a "$BACKEND_LOG") 2>&1 &
  BACKEND_PID=$!
}

wait_for_backend() {
  local attempts=0
  while [ "$attempts" -lt 240 ]; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "Backend exited before becoming healthy."
      wait "$BACKEND_PID" || true
      return 1
    fi
    if curl -fsS "http://127.0.0.1:$BACKEND_PORT/healthz" >/dev/null 2>&1; then
      echo "Backend is healthy."
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 0.25
  done
  echo "Backend did not become healthy within 60 seconds."
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" || true
  return 1
}

run_zai_startup_check() {
  local backend_healthy="${1:-0}"
  local log_path="$BA_HOME/zai_glm52_startup_check.log"

  if [ "${BETTER_AGENT_SKIP_ZAI_STARTUP_CHECK:-0}" = "1" ]; then
    echo "Skipping Z.AI glm-5.2 startup checker because BETTER_AGENT_SKIP_ZAI_STARTUP_CHECK=1."
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

build_frontend ""

PENDING_REFRESH_ID=""
ZAI_STARTUP_CHECK_DONE=0
while true; do
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
    echo "Backend never became healthy — startup checker launched in background to fix+rerun; exiting."
    break
  fi

  if [ -n "$PENDING_REFRESH_ID" ]; then
    build_frontend "$PENDING_REFRESH_ID"
    PENDING_REFRESH_ID=""
  fi

  # Block until uvicorn exits (Ctrl+C, or SIGTERM from the restart flag).
  wait "$BACKEND_PID" || true

  if [ -f "$FLAG" ]; then
    PENDING_REFRESH_ID="$(cat "$FLAG")"
    rm -f "$FLAG"
    echo "Restart requested — restarting backend..."
    continue
  fi

  echo "Backend exited (no restart flag) — stopping."
  break
done
