from __future__ import annotations

from browser_backend_supervisor import main as supervisor_main
from oskeychain import disable_native_user_interaction


def main() -> int:
    disable_native_user_interaction()
    return supervisor_main()


if __name__ == "__main__":
    raise SystemExit(main())
