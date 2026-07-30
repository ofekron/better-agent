#!/usr/bin/env bash
# Better Agent one-command installer entry point.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ofekron/better-agent/main/scripts/bootstrap.sh | bash
#   curl -fsSL .../bootstrap.sh | bash -s -- --mode default --provider claude --yes
#   ./scripts/bootstrap.sh                       # direct invocation after git clone
#   curl -fsSL .../bootstrap.sh | BETTER_AGENT_FROM=readme bash
#
# Environment:
#   BETTER_AGENT_FROM  Optional acquisition channel, one of: readme, landing,
#     hn, ph, devto, yt, gh-trending, dmg, brew, unknown. Passed through
#     untouched; scripts/install_channel.py owns the allowlist and validates
#     it. Unset or unrecognized is recorded as "unknown". Only the channel
#     string is recorded — no host, user, or environment data.
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
INSTALL_LOCK_DIR=""

err() {
  printf 'bootstrap: %s\n' "$*" >&2
}

cleanup_install_state() {
  if [ -n "$INSTALL_STAGING" ] && [ -e "$INSTALL_STAGING" ]; then
    rm -rf "$INSTALL_STAGING"
  fi
  if [ -n "$INSTALL_LOCK_DIR" ] && [ -d "$INSTALL_LOCK_DIR" ]; then
    rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
  fi
}

trap cleanup_install_state EXIT HUP INT TERM

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

# Concurrency-safe: `mkdir` on the lock dir is atomic (fails if it already
# exists), so it's a correct mutex across two installers racing on a fresh
# INSTALL_DIR — unlike the plain "check-then-mv" this replaced, which had a
# TOCTOU gap: both racers can see INSTALL_DIR absent, both clone, and the
# loser's `mv staging INSTALL_DIR` lands *inside* the winner's already-moved
# directory instead of erroring (POSIX `mv` moves INTO an existing directory
# rather than failing), leaving a orphaned nested
# INSTALL_DIR/checkout.installing.<pid> that nothing cleans up. The lock
# makes the loser wait for the winner instead of racing the same `mv`.
sync_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    printf 'bootstrap: updating existing checkout at %s\n' "$INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
    return
  fi

  local parent lock_dir
  parent="$(dirname "$INSTALL_DIR")"
  lock_dir="${INSTALL_DIR}.lock"
  mkdir -p "$parent"

  # Loop rather than fall through once on a released-but-empty lock: if the
  # holder acquired the lock then died before publishing (clone failed,
  # process killed) with MULTIPLE waiters parked, a single unconditional
  # fall-through would let every waiter race the same unlocked clone+mv —
  # exactly the TOCTOU bug this lock exists to prevent. Re-attempting mkdir
  # means only one of them wins the retry.
  local total_waited=0
  while ! mkdir "$lock_dir" 2>/dev/null; do
    printf 'bootstrap: another installer is publishing %s — waiting...\n' "$INSTALL_DIR" >&2
    while [ -d "$lock_dir" ]; do
      if [ -d "$INSTALL_DIR/.git" ]; then
        printf 'bootstrap: %s was published by the other installer\n' "$INSTALL_DIR" >&2
        return
      fi
      total_waited=$((total_waited + 1))
      if [ "$total_waited" -ge 120 ]; then
        err "timed out waiting for a concurrent installer to finish publishing ${INSTALL_DIR}"
        exit 1
      fi
      sleep 1
    done
    if [ -d "$INSTALL_DIR/.git" ]; then
      return
    fi
    # Lock released but no checkout appeared — the previous holder failed.
    # Loop back and race for the lock ourselves instead of cloning unlocked.
  done
  INSTALL_LOCK_DIR="$lock_dir"

  if [ -e "$INSTALL_DIR" ]; then
    err "install destination exists but is not a Git checkout: ${INSTALL_DIR}. Move it aside or set BETTER_AGENT_INSTALL_DIR, then retry. No files were changed."
    exit 1
  fi

  local staging
  staging="${INSTALL_DIR}.installing.$$"
  INSTALL_STAGING="$staging"

  printf 'bootstrap: cloning %s to %s\n' "$REPO_URL" "$INSTALL_DIR"
  if ! git clone "$REPO_URL" "$staging"; then
    err "clone failed; no partial installation was published"
    exit 1
  fi

  if ! mv "$staging" "$INSTALL_DIR"; then
    err "could not publish completed checkout at ${INSTALL_DIR}"
    exit 1
  fi
  INSTALL_STAGING=""
  rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
  INSTALL_LOCK_DIR=""
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
