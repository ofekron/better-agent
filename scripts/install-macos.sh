#!/bin/bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "install-macos.sh only supports macOS." >&2
  exit 1
fi

if ! xcode-select -p >/dev/null 2>&1; then
  echo "Installing Xcode command line tools. Re-run this script after the installer finishes."
  xcode-select --install
  exit 1
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

python3 "$(dirname "$0")/install.py" "$@"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ACTIVE_ENV="$(python3 "$REPO/backend/dependency_plan.py" activate --uv "$(command -v uv)")"
BETTER_AGENT_BACKEND_PYTHON="$ACTIVE_ENV/bin/python" \
  "$REPO/scripts/install-bagent.sh"

git --version
python3 --version
uv --version
node --version
npm --version

echo "Installation complete. Run ./run.sh next."
