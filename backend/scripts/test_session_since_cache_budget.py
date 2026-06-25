from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-since-cache-budget-")

import session_manager as session_manager_mod  # noqa: E402
from session_manager import SessionManager, _jsonish_byte_size  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _snapshot(label: str, size: int) -> dict:
    return {
        "messages": [
            {"id": f"{label}-msg", "role": "user", "content": label * size}
        ],
        "next_seq": 1,
    }


def _run() -> bool:
    original_budget = session_manager_mod._SINCE_CACHE_MAX_BYTES
    results: list[tuple[str, bool, str]] = []
    try:
        manager = SessionManager()
        first = _snapshot("a", 800)
        second = _snapshot("b", 800)
        first_size = _jsonish_byte_size(first)
        session_manager_mod._SINCE_CACHE_MAX_BYTES = first_size + 128

        manager._remember_since_cache_snapshot("sid-a", (1, 0, 0), first, first_size)
        second_size = _jsonish_byte_size(second)
        manager._remember_since_cache_snapshot("sid-b", (1, 0, 0), second, second_size)

        results.append((
            "since cache evicts older snapshots by byte budget",
            "sid-a" not in manager._since_cache and "sid-b" in manager._since_cache,
            f"cached={list(manager._since_cache)}",
        ))
        results.append((
            "since cache byte accounting stays within budget",
            manager._since_cache_total_bytes <= session_manager_mod._SINCE_CACHE_MAX_BYTES,
            (
                f"bytes={manager._since_cache_total_bytes} "
                f"budget={session_manager_mod._SINCE_CACHE_MAX_BYTES}"
            ),
        ))

        oversized = SessionManager()
        session_manager_mod._SINCE_CACHE_MAX_BYTES = 1
        huge = _snapshot("huge", 100)
        oversized._remember_since_cache_snapshot(
            "sid-huge", (1, 0, 0), huge, _jsonish_byte_size(huge),
        )
        results.append((
            "oversized since snapshot is not retained",
            oversized._since_cache == {} and oversized._since_cache_total_bytes == 0,
            f"cached={list(oversized._since_cache)} bytes={oversized._since_cache_total_bytes}",
        ))
    finally:
        session_manager_mod._SINCE_CACHE_MAX_BYTES = original_budget

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
