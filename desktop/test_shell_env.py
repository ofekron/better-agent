"""Test desktop/shell_env.py — login-shell PATH capture.

Run with:
    .venv/bin/python desktop/test_shell_env.py
(any interpreter — the module has no third-party deps).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from shell_env import _merge_path_entries, capture_login_path

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_returns_nonempty_path() -> bool:
    p = capture_login_path()
    if not (isinstance(p, str) and p):
        print(f"  expected a non-empty str, got {p!r}")
        return False
    return True


def test_captured_path_resolves_claude() -> bool:
    """The captured login PATH must resolve `claude` where launchd's
    stripped PATH would not — closing that gap is the point of capture."""
    captured = capture_login_path()
    stripped = "/usr/bin:/bin:/usr/sbin:/sbin"
    if shutil.which("claude", path=stripped) is not None:
        # `claude` happens to sit in a system dir — capture is moot here.
        return True
    if shutil.which("claude", path=captured) is None:
        print("  `claude` not resolvable via the captured PATH "
              "(is the Claude CLI installed?)")
        return False
    return True


def test_merge_dedups_preserving_order() -> bool:
    """The Windows merge must de-dup entries, keep first-seen order, and
    drop empties — across the process PATH + registry PATH fragments."""
    sep = os.pathsep
    chunks = [
        sep.join(["a", "b", ""]),     # process PATH (trailing empty)
        sep.join(["b", "c"]),         # user registry PATH (b duplicates)
        sep.join(["c", "d"]),         # system registry PATH (c duplicates)
    ]
    got = _merge_path_entries(chunks)
    want = sep.join(["a", "b", "c", "d"])
    if got != want:
        print(f"  expected {want!r}, got {got!r}")
        return False
    if _merge_path_entries([]) != "" or _merge_path_entries(["", ""]) != "":
        print("  empty inputs must yield an empty PATH")
        return False
    return True


TESTS = [
    ("capture_login_path returns a non-empty PATH", test_returns_nonempty_path),
    ("captured PATH resolves the claude CLI", test_captured_path_resolves_claude),
    ("_merge_path_entries dedups, orders, drops empties",
     test_merge_dedups_preserving_order),
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
