from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit("script path is required")
    script = Path(args[0]).resolve()
    if not script.is_file():
        raise SystemExit(f"script does not exist: {script}")
    previous_argv = sys.argv
    try:
        sys.argv = [str(script), *args[1:]]
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = previous_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
