"""Locks the two import-runtime fixes:

1. Native `created_at` is preserved onto the session record (so usage
   analytics bucket imported sessions under their real date, not import time).
2. The resident-root cache stays bounded during a bulk create loop via
   `session_manager.trim_resident_roots` (no unbounded RAM growth / OOM).

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_runtime.py
"""

from __future__ import annotations

import logging
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-runtime-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

logging.getLogger("config_store").setLevel(logging.CRITICAL)
logging.getLogger("keyring").setLevel(logging.CRITICAL)
from session_manager import manager as session_manager  # noqa: E402
import native_import as ni  # noqa: E402

CASES = {"n": 0}


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    CASES["n"] += 1


def test_created_at_preserved() -> None:
    rec = session_manager.create(name="x", created_at="2025-06-01T12:00:00")
    check(rec["created_at"] == "2025-06-01T12:00:00", f"created_at not preserved: {rec['created_at']}")
    # default still stamps now() when not supplied
    rec2 = session_manager.create(name="y")
    check(rec2["created_at"] != "2025-06-01T12:00:00" and rec2["created_at"], "default created_at present")


def test_native_created_iso() -> None:
    f = ni._native_created_iso
    check(f(ni.NativeSession("p", "claude", "n", "/x.jsonl", created_at="")) is None, "empty → None")
    check(f(ni.NativeSession("p", "claude", "n", "/x.jsonl", created_at="garbage")) is None, "garbage → None")
    # naive passes through; trailing-Z UTC parses to a non-None local iso
    naive = f(ni.NativeSession("p", "claude", "n", "/x.jsonl", created_at="2025-03-04T05:06:07"))
    check(naive == "2025-03-04T05:06:07", f"naive passthrough: {naive}")
    z = f(ni.NativeSession("p", "claude", "n", "/x.jsonl", created_at="2026-01-01T00:00:00Z"))
    check(z is not None and "Z" not in z and "+" not in z, f"Z normalized to naive local: {z}")


def test_resident_cache_bounded() -> None:
    cap = session_manager._roots_max
    # Model an idle import: the orchestrator pin predicate (absent in this
    # harness → fails closed/pinned) reports nothing pinned for idle sessions.
    session_manager._pin_predicate = lambda rid, node_sids: False
    try:
        for i in range(cap + 15):
            rec = session_manager.create(name=f"s{i}")
            session_manager.trim_resident_roots(keep_rid=rec["id"])
        check(len(session_manager._roots) <= cap,
              f"resident roots {len(session_manager._roots)} exceeds cap {cap}")
    finally:
        session_manager._pin_predicate = None


def main() -> None:
    test_created_at_preserved()
    test_native_created_iso()
    test_resident_cache_bounded()
    print(f"OK — {CASES['n']} checks passed")


if __name__ == "__main__":
    main()
