#!/bin/bash

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
DEFAULT_VENV="$REPO/backend/.venv"
PYTHON="${BETTER_AGENT_BACKEND_PYTHON:-$DEFAULT_VENV/bin/python}"
VENV="$(dirname "$(dirname "$PYTHON")")"
TARGET="$DIR/dist/BetterAgentCredentialAuthority/BetterAgentCredentialAuthority"

if [ "${BETTER_AGENT_CREDENTIAL_BUILD_LOCKED:-0}" != "1" ]; then
  mkdir -p "$DIR/build"
  export BETTER_AGENT_CREDENTIAL_BUILD_LOCKED=1
  exec "$PYTHON" "$DIR/credential_build_lock.py" \
    "$DIR/build/.credential-authority.lock" bash "$0" "$@"
fi

SOURCES=(
  "$DIR/CredentialAuthority.spec"
  "$DIR/credential_supervisor_main.py"
  "$DIR/browser_backend_supervisor.py"
  "$DIR/backend_process_owner.py"
  "$DIR/credential_session.py"
  "$REPO/backend/headless_keyring.py"
  "$REPO/backend/provider_credentials.py"
  "$REPO/backend/oskeychain.py"
  "$REPO/backend/primary_launcher_lease.py"
)

needs_build=0
if [ ! -x "$TARGET" ]; then
  needs_build=1
elif ! "$TARGET" --self-test >/dev/null 2>&1; then
  needs_build=1
else
  for source in "${SOURCES[@]}"; do
    if [ "$source" -nt "$TARGET" ]; then
      needs_build=1
      break
    fi
  done
fi

if [ "$needs_build" -eq 1 ]; then
  if [ ! -x "$VENV/bin/pyinstaller" ]; then
    UV="$(command -v uv || printf '%s' "$HOME/.local/bin/uv")"
    (cd "$REPO/backend" && "$UV" pip install -q --python "$VENV/bin/python" pyinstaller)
  fi
  rm -rf "$DIR/build/CredentialAuthority" "$DIR/dist/BetterAgentCredentialAuthority"
  (cd "$DIR" && "$VENV/bin/pyinstaller" --noconfirm CredentialAuthority.spec)

  if [ "$(uname -s)" = "Darwin" ]; then
    if ! bash "$DIR/local_codesign.sh" verify "$TARGET" >/dev/null 2>&1; then
      bash "$DIR/local_codesign.sh" sign "$TARGET"
    fi
  fi

  "$TARGET" --self-test
fi

printf '%s\n' "$TARGET"
