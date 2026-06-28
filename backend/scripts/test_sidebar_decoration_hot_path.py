from __future__ import annotations

import os
import sys

import _test_home

_test_home.isolate("bc-test-sidebar-hot-path-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402


def test_sidebar_decoration_uses_summary_error_projection() -> bool:
    original = main.session_manager.has_unseen_error

    def fail_load(_sid: str) -> bool:
        raise AssertionError("sidebar decoration loaded full session for error state")

    main.session_manager.has_unseen_error = fail_load
    try:
        rows = main._decorate_local_sidebar_sessions([{
            "id": "sid-hot-path",
            "name": "hot path",
            "cwd": "/tmp",
            "node_id": "primary",
            "unseen_error": "boom",
        }])
    finally:
        main.session_manager.has_unseen_error = original
    return len(rows) == 1 and rows[0].get("has_error") is True


def run() -> int:
    tests = [
        (
            "sidebar decoration uses summary error projection",
            test_sidebar_decoration_uses_summary_error_projection,
        ),
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"FAIL {name}: {exc}")
        else:
            print(("PASS" if ok else "FAIL") + f" {name}")
        if not ok:
            failures.append(name)
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
