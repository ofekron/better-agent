"""Regression: turn_start frames must name their Better Agent session."""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def main() -> int:
    src = open(os.path.join(BACKEND, "turn_manager.py"), encoding="utf-8").read()
    matches = re.findall(
        r'"type":\s*"turn_start"\s*,\s*"data":\s*\{([^}]*)',
        src,
    )
    if not matches:
        print(f"{FAIL} no turn_start emit found")
        return 1
    ok = all('"app_session_id": app_session_id' in body for body in matches)
    print(
        f"{PASS if ok else FAIL} turn_start emits app_session_id on every path"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
