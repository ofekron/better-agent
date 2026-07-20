"""Test desktop/app_main.py — frozen-app role dispatch.

Run with:
    backend/.venv/bin/python desktop/test_app_main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
for _p in (_HERE, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import app_main
from app_main import _role

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_role_dispatch() -> bool:
    cases = {
        (): "shell",
        ("--serve",): "backend",
        ("--serve-node",): "backend",
        ("--run-dir", "/runs/x"): "backend",
        ("--run-dir", "/runs/x", "--runner-kind", "gemini"): "backend",
    }
    for argv, expected in cases.items():
        got = _role(list(argv))
        if got != expected:
            print(f"  {argv}: expected {expected!r}, got {got!r}")
            return False
    return True


TESTS = [
    ("app_main._role classifies shell vs backend invocations",
     test_role_dispatch),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
