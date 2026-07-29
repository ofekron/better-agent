from __future__ import annotations

import importlib
import runpy
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        raise SystemExit("package root and script path are required")
    package_root = Path(args[0]).resolve()
    script = Path(args[1]).resolve()
    if not package_root.is_dir():
        raise SystemExit(f"package root does not exist: {package_root}")
    if not script.is_file():
        raise SystemExit(f"script does not exist: {script}")
    if not script.is_relative_to(package_root):
        raise SystemExit(f"script is outside package root: {script}")
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
