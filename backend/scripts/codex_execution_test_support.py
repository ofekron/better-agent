from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace


def write_executable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def provider(config_dir: Path) -> dict:
    return {
        "id": "codex-provider",
        "kind": "codex",
        "generation": "generation-a",
        "revision": 7,
        "execution_revision": 3,
        "config_dir": str(config_dir),
        "base_url": "",
        "mode": "subscription",
        "api_key": "must-never-persist",
    }


def runner_authority(run_dir: Path):
    codex_home = run_dir / "source-codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    contract = SimpleNamespace(
        config=(),
        environment_selectors=(("CODEX_HOME", str(codex_home)),),
        profile="",
        runtime_args=(),
    )
    launch = SimpleNamespace(argv_prefix=("codex",), pass_fds=())
    return contract, launch
