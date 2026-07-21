from __future__ import annotations

import sys


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return 0
    from browser_backend_supervisor import main as supervisor_main

    return supervisor_main()


if __name__ == "__main__":
    raise SystemExit(main())
