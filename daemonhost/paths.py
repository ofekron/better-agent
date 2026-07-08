"""State paths for the daemon host.

The host cannot import ``backend.paths`` (it must run without the backend's
package or venv), so it mirrors the same env chain run.sh uses:
BETTER_AGENT_HOME, then legacy BETTER_CLAUDE_HOME, then ~/.better-claude.
"""

from __future__ import annotations

import os
from pathlib import Path


def ba_home() -> Path:
    for var in ("BETTER_AGENT_HOME", "BETTER_CLAUDE_HOME"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".better-claude"


def daemons_root() -> Path:
    return ba_home() / "daemons"


def registry_path() -> Path:
    return daemons_root() / "registry.json"


def state_path() -> Path:
    return daemons_root() / "state.json"


def daemon_root(extension_id: str, name: str) -> Path:
    return daemons_root() / extension_id / name


def logs_root() -> Path:
    return daemons_root() / "logs"


def pointer_path() -> Path:
    return ba_home() / "active_checkout.json"
