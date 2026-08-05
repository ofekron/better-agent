from __future__ import annotations

import site
import sys
from pathlib import Path

from backend_instance_lock import adopt_primary_backend_instance_lock


def main() -> int:
    adopt_primary_backend_instance_lock()
    site.main()
    root = Path(__file__).resolve().parents[1]
    for path in (root, root / "backend", root / "sdk"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    from app_entry import _main

    return _main(["--serve"])


if __name__ == "__main__":
    raise SystemExit(main())
