from __future__ import annotations

import importlib
import importlib.util
import hashlib
import runpy
import sys
from types import ModuleType
from pathlib import Path


def _validated_paths(package_root: str | Path, script: str | Path) -> tuple[Path, Path]:
    root = Path(package_root).resolve()
    target = Path(script).resolve()
    if not root.is_dir():
        raise RuntimeError(f"package root does not exist: {root}")
    if not target.is_file():
        raise RuntimeError(f"script does not exist: {target}")
    if not target.is_relative_to(root):
        raise RuntimeError(f"script is outside package root: {target}")
    return root, target


def load_script_module(
    package_root: str | Path,
    script: str | Path,
    argv: list[str] | None = None,
) -> ModuleType:
    _root, target = _validated_paths(package_root, script)
    module_name = "_better_agent_extension_" + hashlib.sha256(
        str(target).encode("utf-8")
    ).hexdigest()
    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load extension script: {target}")
    module = importlib.util.module_from_spec(spec)
    previous_argv = sys.argv
    previous_module = sys.modules.get(module_name)
    try:
        sys.modules[module_name] = module
        sys.argv = [str(target), *(argv or [])]
        spec.loader.exec_module(module)
    except BaseException:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
        raise
    finally:
        sys.argv = previous_argv
    return module


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        raise SystemExit("package root and script path are required")
    try:
        package_root, script = _validated_paths(args[0], args[1])
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    original_path = list(sys.path)
    try:
        sys.path[:] = [
            entry
            for entry in sys.path
            if Path(entry or ".").resolve() != package_root
        ]
        importlib.import_module("mcp.server.fastmcp")
    finally:
        sys.path[:] = original_path
    previous_argv = sys.argv
    try:
        sys.argv = [str(script), *args[2:]]
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = previous_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
