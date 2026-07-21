from __future__ import annotations

from browser_backend_supervisor import main as supervisor_main


def main() -> int:
    return supervisor_main()


if __name__ == "__main__":
    raise SystemExit(main())
