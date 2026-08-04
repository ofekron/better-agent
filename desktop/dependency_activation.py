from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from dependency_environment import (
    dependency_subprocess_env,
    python_in,
    verified_active_env,
)


class DependencyActivationError(RuntimeError):
    pass


def _checkout_backend(checkout: Path) -> Path:
    candidate = checkout.expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
        raise DependencyActivationError("target checkout path is invalid")
    resolved = candidate.resolve()
    backend = resolved / "backend"
    if not (backend / "main.py").is_file() or not (
        backend / "dependency_plan.py"
    ).is_file():
        raise DependencyActivationError("target checkout is not runnable")
    return backend


def _uv_executable(uv: str, env: Mapping[str, str]) -> Path:
    candidate = Path(uv).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(uv, path=env.get("PATH"))
        if not found:
            raise DependencyActivationError("uv executable is unavailable")
        candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DependencyActivationError("uv executable is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise DependencyActivationError("uv executable is unavailable")
    return resolved


def activate_checkout(
    checkout: Path,
    uv: str,
    *,
    source_env: Mapping[str, str] | None = None,
) -> Path:
    backend = _checkout_backend(checkout)
    env = dependency_subprocess_env(os.environ if source_env is None else source_env)
    uv_path = _uv_executable(uv, env)
    planner = (backend / "dependency_plan.py").resolve()
    try:
        subprocess.run(
            [
                str(Path(sys.executable).resolve()),
                str(planner),
                "activate",
                "--uv",
                str(uv_path),
            ],
            cwd=backend,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyActivationError(
            "target checkout dependency activation failed"
        ) from exc
    return python_in(verified_active_env(backend, source_env=env))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--uv", required=True)
    args = parser.parse_args(argv)
    print(activate_checkout(args.checkout, args.uv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
