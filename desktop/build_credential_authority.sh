#!/bin/bash

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
VENV="$REPO/backend/.venv"
TARGET="$DIR/dist/BetterAgentCredentialAuthority/BetterAgentCredentialAuthority"

if [ "${BETTER_AGENT_CREDENTIAL_BUILD_LOCKED:-0}" != "1" ]; then
  mkdir -p "$DIR/build"
  export BETTER_AGENT_CREDENTIAL_BUILD_LOCKED=1
  exec "$VENV/bin/python" "$DIR/credential_build_lock.py" \
    "$DIR/build/.credential-authority.lock" "$0" "$@"
fi

SOURCES=(
  "$DIR/CredentialAuthority.spec"
  "$DIR/credential_supervisor_main.py"
  "$DIR/browser_backend_supervisor.py"
  "$DIR/credential_session.py"
  "$REPO/backend/provider_credentials.py"
  "$REPO/backend/oskeychain.py"
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
fi

if ! "$DIR/local_codesign.sh" verify "$TARGET" >/dev/null 2>&1; then
  "$DIR/local_codesign.sh" sign "$TARGET"
fi

"$TARGET" --self-test

printf '%s\n' "$TARGET"
