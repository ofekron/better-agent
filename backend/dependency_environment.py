from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Mapping

ACTIVE_POINTER_NAME = ".active-venv"
VENV_ROOT_NAME = ".venvs"
PLAN_MARKER = ".dependency-plan.json"
_DEPENDENCY_ENV_KEYS = frozenset({
    "ALL_PROXY",
    "APPDATA",
    "BETTER_AGENT_HOME",
    "BETTER_AGENT_NODE_ID",
    "BETTER_CLAUDE_HOME",
    "BETTER_CLAUDE_NODE_ID",
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "UV_CACHE_DIR",
    "UV_HTTP_TIMEOUT",
    "UV_NATIVE_TLS",
    "UV_NO_CACHE",
    "UV_OFFLINE",
    "WINDIR",
    "XDG_CACHE_HOME",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
})


class DependencyEnvironmentError(RuntimeError):
    pass


def python_in(env_dir: Path) -> Path:
    if os.name == "nt":  # pragma: no cover - Windows-only path flavour; flipping os.name corrupts the POSIX Path factory
        return env_dir / "Scripts/python.exe"
    return env_dir / "bin/python"


def active_env(backend_dir: Path) -> Path:
    pointer = backend_dir / ACTIVE_POINTER_NAME
    venv_root = backend_dir / VENV_ROOT_NAME
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DependencyEnvironmentError(
            "backend dependency environment is not activated"
        ) from exc
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise DependencyEnvironmentError(
            "backend dependency environment pointer is invalid"
        )
    env_dir = (backend_dir / relative).resolve()
    runnable = any(
        (env_dir / executable).is_file()
        for executable in ("bin/python", "Scripts/python.exe")
    )
    if venv_root.resolve() not in env_dir.parents or not runnable:
        raise DependencyEnvironmentError(
            "backend dependency environment pointer is not runnable"
        )
    return env_dir


def dependency_subprocess_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    return {
        key: value
        for key, value in values.items()
        if key in _DEPENDENCY_ENV_KEYS or key == "LANG" or key.startswith("LC_")
    }


def active_runtime_env(backend_dir: Path) -> Path:
    env_dir = active_env(backend_dir)
    try:
        marker = json.loads((env_dir / PLAN_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyEnvironmentError(
            "backend dependency environment activation receipt is invalid"
        ) from exc
    if (
        not isinstance(marker, dict)
        or marker.get("schema_version") != 1
        or not isinstance(marker.get("hash"), str)
        or not marker["hash"]
    ):
        raise DependencyEnvironmentError(
            "backend dependency environment activation receipt is invalid"
        )
    try:
        subprocess.run(
            [str(python_in(env_dir)), "-c", "import sys; sys.exit(0)"],
            cwd=backend_dir,
            env=dependency_subprocess_env(),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyEnvironmentError(
            "backend dependency environment pointer is not runnable"
        ) from exc
    return env_dir


def verified_active_env(
    backend_dir: Path,
    *,
    source_env: Mapping[str, str] | None = None,
) -> Path:
    env_dir = active_env(backend_dir)
    planner = backend_dir / "dependency_plan.py"
    if not planner.is_file():
        raise DependencyEnvironmentError("target checkout has no dependency planner")
    try:
        subprocess.run(
            [str(python_in(env_dir)), str(planner), "assert-active-plan"],
            cwd=backend_dir,
            env=dependency_subprocess_env(source_env),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyEnvironmentError(
            "target checkout dependency environment is stale"
        ) from exc
    return env_dir


def verified_active_python(backend_dir: Path) -> Path:
    return python_in(verified_active_env(backend_dir))


def active_runtime_python(backend_dir: Path) -> Path:
    return python_in(active_runtime_env(backend_dir))
