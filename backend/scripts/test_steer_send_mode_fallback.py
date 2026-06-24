from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
TMP_HOME = _test_home.isolate("bc-test-steer-fallback-")

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def check(name: str, got: str, expected: str) -> bool:
    ok = got == expected
    print(f"{PASS if ok else FAIL} {name}: got={got!r} expected={expected!r}")
    return ok


def main() -> int:
    try:
        import main as backend_main

        ok = True
        ok &= check(
            "stale steer falls back to normal send",
            backend_main._normalize_ws_send_mode_for_turn_state("steer", False),
            "queue",
        )
        ok &= check(
            "active steer stays steer",
            backend_main._normalize_ws_send_mode_for_turn_state("steer", True),
            "steer",
        )
        ok &= check(
            "queue stays queue",
            backend_main._normalize_ws_send_mode_for_turn_state("queue", False),
            "queue",
        )
        ok &= check(
            "failed active steer falls back to queue",
            backend_main._fallback_ws_send_mode_after_failed_steer("steer"),
            "queue",
        )
        return 0 if ok else 1
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
