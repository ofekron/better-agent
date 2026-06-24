#!/bin/bash
# Better Agent — worker-node launcher.
#
# This is the node-mode counterpart to run.sh. run.sh launches the
# PRIMARY web-UI host (uvicorn main:app) and bootstraps browser-login
# credentials in the macOS keychain. A NODE never serves the web UI and
# never logs a human in — it dials OUT to the primary, presenting its
# persistent identity secret, and waits for an operator to approve it in
# the primary's UI. So this script deliberately:
#
#   - launches `uvicorn main_node:app` (not main:app)
#   - has NO keychain bootstrap (a node has no username/password/secret)
#   - does NOT build or serve the frontend (no web UI on a node)
#   - does NOT install the `bagent` CLI (TestApe/UI-only)
#   - has NO restart loop — main_node exposes no /api/admin/restart flag;
#     it runs in the foreground and Ctrl+C stops it.
#
# On a bare machine this script auto-installs everything it needs:
# Python 3.11+, uv, the venv, and pip dependencies. It will also
# generate a topology.yaml interactively if one is missing.
#
# What a node needs to start:
#   - BETTER_AGENT_TOPOLOGY_PATH → a topology.yaml whose `primary.address`
#     is the primary's WS URL (e.g. ws://primary.local:8001). If missing,
#     the script generates one interactively.
#   - BETTER_AGENT_NODE_ID → this node's id (default: hostname). Must
#     differ from the primary's id or main_node refuses to start.
#   - BETTER_AGENT_NODE_TOKEN → optional; only when this node is listed
#     statically in topology.yaml. Otherwise the node self-generates a
#     secret at $BETTER_AGENT_HOME/node_identity.json and is approved by
#     an operator in the primary UI on first connect.
#   - BETTER_AGENT_NODE_PORT → local port to bind (default: 8002).

set -e
set -o pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$DIR/backend"
BA_HOME="${BETTER_AGENT_HOME:-${BETTER_CLAUDE_HOME:-$HOME/.better-claude}}"
export BETTER_AGENT_HOME="${BETTER_AGENT_HOME:-$BA_HOME}"
export BETTER_CLAUDE_HOME="${BETTER_CLAUDE_HOME:-$BA_HOME}"
NODE_PORT="${BETTER_AGENT_NODE_PORT:-${BETTER_CLAUDE_NODE_PORT:-8002}}"

mkdir -p "$BA_HOME"

# ============================================================================
# Auto-install: Python 3.11+, uv, venv, pip deps
# ============================================================================

_detect_python() {
  # Prefer an explicit override, then try common names.
  local candidates=("python3" "python3.12" "python3.11" "python")
  local c
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1; then
      local ver
      ver=$("$c" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
      local major minor
      IFS='.' read -r major minor <<< "$ver"
      if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
        echo "$c"
        return 0
      fi
    fi
  done
  return 1
}

_install_python() {
  echo "No Python 3.11+ found — installing..."
  if [ "$(uname -s)" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      brew install python@3.12
    else
      echo "ERROR: No Homebrew found. Install Homebrew first:" >&2
      echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"" >&2
      exit 1
    fi
  else
    # Linux — try apt, then dnf, then apk.
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq
      sudo apt-get install -y -qq python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y python3 python3-pip
    elif command -v apk >/dev/null 2>&1; then
      sudo apk add python3 py3-pip
    else
      echo "ERROR: Unsupported package manager. Install Python 3.11+ manually." >&2
      exit 1
    fi
  fi
}

_install_uv() {
  echo "Installing uv (Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin by default.
  export PATH="$HOME/.local/bin:$PATH"
}

kill_port_listeners() {
  local port="$1"
  local pids=""
  local attempts=0

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
    echo "Force killing remaining PIDs on :$port..." >&2
    echo "$pids" | xargs kill -9 2>/dev/null || true
  fi
  sleep 0.5
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
  if [ -n "$pids" ]; then
    echo "Port :$port is still occupied by listener PID(s):" >&2
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
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

PYTHON="$(_detect_python)" || true
if [ -z "$PYTHON" ]; then
  _install_python
  PYTHON="$(_detect_python)" || { echo "ERROR: Python still not found after install." >&2; exit 1; }
fi
echo "Python: $PYTHON ($($PYTHON --version 2>&1))"

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  _install_uv
fi
echo "uv: $UV ($($UV --version 2>&1))"

if [ ! -d "$BACKEND/.venv" ]; then
  echo "Creating Python venv..."
  "$UV" venv "$BACKEND/.venv" --python "$PYTHON"
fi

PY="$BACKEND/.venv/bin/python"
UVICORN="$BACKEND/.venv/bin/uvicorn"

# ============================================================================
# Topology — generate interactively if missing
# ============================================================================

TOPOLOGY_PATH="${BETTER_AGENT_TOPOLOGY_PATH:-${BETTER_CLAUDE_TOPOLOGY_PATH:-$BA_HOME/topology.yaml}}"
export BETTER_AGENT_TOPOLOGY_PATH="$TOPOLOGY_PATH"
export BETTER_CLAUDE_TOPOLOGY_PATH="$TOPOLOGY_PATH"
if [ ! -f "$BETTER_CLAUDE_TOPOLOGY_PATH" ]; then
  echo
  echo "No topology file found at $BETTER_CLAUDE_TOPOLOGY_PATH"
  echo "Let's generate one."
  echo

  read -p "Primary address [ws://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):8000]: " PRIMARY_ADDR
  if [ -z "$PRIMARY_ADDR" ]; then
    echo "ERROR: Primary address is required." >&2
    exit 1
  fi

  # Default port to 8000 if bare IP/hostname given without port
  case "$PRIMARY_ADDR" in
    ws://*|wss://*)
      # Already has scheme — check for port
      ;;
    *)
      PRIMARY_ADDR="ws://$PRIMARY_ADDR"
      ;;
  esac
  # Add :8000 if no port specified
  if ! echo "$PRIMARY_ADDR" | grep -qE ':[0-9]+(/|$)'; then
    PRIMARY_ADDR="$PRIMARY_ADDR:8000"
  fi
  if [ -z "$PRIMARY_ADDR" ]; then
    echo "ERROR: Primary address is required." >&2
    exit 1
  fi

  cat > "$BETTER_CLAUDE_TOPOLOGY_PATH" <<EOF
schema_version: 1
primary:
  id: primary
  address: $PRIMARY_ADDR
  cwd_roots: []
nodes: {}
EOF
  chmod 600 "$BETTER_CLAUDE_TOPOLOGY_PATH"
  echo
  echo "Generated $BETTER_CLAUDE_TOPOLOGY_PATH"
  echo
fi

# ============================================================================
# Start
# ============================================================================

NODE_PORT="$(resolve_port_conflict "$NODE_PORT" "node")"
export BETTER_CLAUDE_NODE_PORT="$NODE_PORT"
export BETTER_AGENT_NODE_PORT="$NODE_PORT"

echo "Syncing backend deps..."
"$UV" pip install -q --python "$PY" -r "$BACKEND/requirements.txt"

echo "Starting node (main_node:app) on :$NODE_PORT..."
echo "  node_id : ${BETTER_AGENT_NODE_ID:-${BETTER_CLAUDE_NODE_ID:-$(hostname)}}"
echo "  topology: $BETTER_CLAUDE_TOPOLOGY_PATH"
cd "$BACKEND"
exec "$UVICORN" main_node:app --host 0.0.0.0 --port "$NODE_PORT"
