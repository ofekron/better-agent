from __future__ import annotations

import stat
from pathlib import Path


def write_executable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def provider(config_dir: Path) -> dict:
    return {
        "id": "codex-provider",
        "kind": "codex",
        "generation": "generation-a",
        "record_version": 7,
        "config_dir": str(config_dir),
        "base_url": "",
        "mode": "subscription",
        "api_key": "must-never-persist",
    }
