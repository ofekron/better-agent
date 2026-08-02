from __future__ import annotations

import os
import subprocess
from pathlib import Path

ACTIVE_POINTER_NAME = ".active-venv"
VENV_ROOT_NAME = ".venvs"
PLAN_MARKER = ".dependency-plan.json"


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


def verified_active_env(backend_dir: Path) -> Path:
    env_dir = active_env(backend_dir)
    planner = backend_dir / "dependency_plan.py"
    if not planner.is_file():
        raise DependencyEnvironmentError("target checkout has no dependency planner")
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    try:
        subprocess.run(
            [str(python_in(env_dir)), str(planner), "assert-active-plan"],
            cwd=backend_dir,
            env=env,
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
