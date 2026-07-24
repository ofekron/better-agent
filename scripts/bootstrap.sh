#!/usr/bin/env bash
# Better Agent one-command installer entry point.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ofekron/better-agent/main/scripts/bootstrap.sh | bash
#   curl -fsSL .../bootstrap.sh | bash -s -- --mode default --provider claude --yes
#   ./scripts/bootstrap.sh                       # direct invocation after git clone
#
# Behaviour:
#   - macOS only. Windows users run scripts/bootstrap.ps1 instead.
#   - Clones to ~/.better-agent/checkout if absent, git pulls (fast-forward
#     only) if present.
#   - Verifies git is on PATH.
#   - Hands off to scripts/install-macos.sh in that checkout, forwarding
#     every argument (--mode / --provider / --yes) unchanged. That script
#     bootstraps Xcode CLT + Homebrew + uv/node/python, then runs
#     scripts/install.py. See INSTALL.md Part 1.1.

set -euo pipefail

REPO_URL="${BETTER_AGENT_REPO_URL:-https://github.com/ofekron/better-agent.git}"
INSTALL_DIR="${BETTER_AGENT_INSTALL_DIR:-$HOME/.better-agent/checkout}"
INSTALL_STAGING=""

err() {
  printf 'bootstrap: %s\n' "$*" >&2
}

cleanup_install_staging() {
  if [ -n "$INSTALL_STAGING" ] && [ -e "$INSTALL_STAGING" ]; then
    rm -rf "$INSTALL_STAGING"
  fi
}

trap cleanup_install_staging EXIT HUP INT TERM

require_supported_platform() {
  local uname_s
  uname_s="$(uname -s 2>/dev/null || printf 'unknown')"
  if [ "$uname_s" != "Darwin" ]; then
    err "this one-liner supports macOS only. On Windows, run scripts/bootstrap.ps1 in PowerShell. On Linux, clone the repo and follow INSTALL.md manually — there is no packaged one-liner for Linux yet."
    exit 2
  fi
}

require_git() {
  if ! command -v git >/dev/null 2>&1; then
    err "git not found on PATH. Run 'xcode-select --install', then re-run this installer."
    exit 1
  fi
}

# Concurrency-safe: stage the clone next to the destination and only
# publish via atomic rename, so two installers racing each other never
# leave a partial checkout behind or clobber one that already published.
sync_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    printf 'bootstrap: updating existing checkout at %s\n' "$INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
    return
  fi

  if [ -e "$INSTALL_DIR" ]; then
    err "install destination exists but is not a Git checkout: ${INSTALL_DIR}. Move it aside or set BETTER_AGENT_INSTALL_DIR, then retry. No files were changed."
    exit 1
  fi

  local parent staging
  parent="$(dirname "$INSTALL_DIR")"
  staging="${INSTALL_DIR}.installing.$$"
  mkdir -p "$parent"
  INSTALL_STAGING="$staging"

  printf 'bootstrap: cloning %s to %s\n' "$REPO_URL" "$INSTALL_DIR"
  if ! git clone "$REPO_URL" "$staging"; then
    err "clone failed; no partial installation was published"
    exit 1
  fi

  if [ -e "$INSTALL_DIR" ]; then
    err "another installer published ${INSTALL_DIR} while cloning; leaving it untouched"
    exit 1
  fi
  if ! mv "$staging" "$INSTALL_DIR"; then
    err "could not publish completed checkout at ${INSTALL_DIR}"
    exit 1
  fi
  INSTALL_STAGING=""
}

main() {
  require_supported_platform
  require_git
  sync_repo
  printf 'bootstrap: handing off to install-macos.sh\n'
  cd "$INSTALL_DIR"
  exec ./scripts/install-macos.sh "$@"
}

# Only auto-run when executed, not when sourced (tests source this to call
# individual functions directly).
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then
  main "$@"
fi
