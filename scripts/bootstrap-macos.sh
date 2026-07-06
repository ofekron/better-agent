#!/bin/bash
set -euo pipefail

YES=0
WITH_CLAUDE=0
WITH_CODEX=0

usage() {
  echo "Usage: scripts/bootstrap-macos.sh [--yes] [--with-claude] [--with-codex]"
}

for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --with-claude) WITH_CLAUDE=1 ;;
    --with-codex) WITH_CODEX=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "bootstrap-macos.sh only supports macOS." >&2
  exit 1
fi

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Installing Xcode command line tools. Re-run this script after the installer finishes."
  xcode-select --install
  exit 1
fi

if [ "$YES" -ne 1 ]; then
  echo "This installs Homebrew if missing, then installs: git python uv node."
  if [ "$WITH_CLAUDE" -eq 1 ]; then
    echo "It also installs Claude Code CLI globally with npm."
  fi
  if [ "$WITH_CODEX" -eq 1 ]; then
    echo "It also installs Codex CLI globally with npm."
  fi
  read -r -p "Continue? [y/N]: " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x /usr/local/bin/brew ]; then
  eval "$(/usr/local/bin/brew shellenv)"
fi

brew update
brew install git python uv node

if [ "$WITH_CLAUDE" -eq 1 ]; then
  npm install -g @anthropic-ai/claude-code
fi
if [ "$WITH_CODEX" -eq 1 ]; then
  npm install -g @openai/codex
fi

git --version
python3 --version
uv --version
node --version
npm --version

echo "Base macOS prerequisites installed. Run ./run.sh next."
