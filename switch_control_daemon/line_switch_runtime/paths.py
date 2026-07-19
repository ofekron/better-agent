from __future__ import annotations

import os
from pathlib import Path


def ba_home() -> Path:
    for variable in ("BETTER_AGENT_HOME", "BETTER_CLAUDE_HOME"):
        value = os.environ.get(variable, "").strip()
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


def switch_journal_path() -> Path:
    return ba_home() / "switch_journal.jsonl"


def switch_lines_path() -> Path:
    return ba_home() / "switch_lines.json"


def switch_request_path() -> Path:
    return ba_home() / "switch_request.json"


def web_access_path() -> Path:
    return ba_home() / "switch_control_web.json"


def restart_request_path() -> Path:
    return ba_home() / "restart_requested"


def refresh_result_path() -> Path:
    return ba_home() / "refresh_result.json"
