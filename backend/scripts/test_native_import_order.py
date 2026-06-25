"""Locks that the native-import job processes sessions newest-first and
that a `limit` cap therefore keeps the most recent N.

Runs `_run_import` with `enumerate_native_sessions` / `import_session`
stubbed so it exercises only the ordering + cap logic, no real ingest.

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_order.py
"""

from __future__ import annotations

import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-order-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import native_import as ni  # noqa: E402

CASES = {"n": 0}


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    CASES["n"] += 1


def _sess(nid: str, created_at: str) -> ni.NativeSession:
    return ni.NativeSession(
        provider_id="p", provider_kind="claude", native_id=nid,
        jsonl_path=f"/tmp/{nid}.jsonl", created_at=created_at,
    )


def main() -> None:
    # Scrambled input order; created_at out of order, one unknown ("").
    scrambled = [
        _sess("mid", "2026-03-01T00:00:00Z"),
        _sess("newest", "2026-06-01T00:00:00Z"),
        _sess("unknown", ""),
        _sess("oldest", "2026-01-01T00:00:00Z"),
        _sess("second", "2026-05-01T00:00:00Z"),
    ]
    expected_desc = ["newest", "second", "mid", "oldest", "unknown"]

    seen: list[str] = []
    orig_enum = ni.enumerate_native_sessions
    orig_import = ni.import_session
    orig_keys = ni.already_imported_keys
    ni.enumerate_native_sessions = lambda *a, **k: list(scrambled)
    ni.import_session = lambda sess: seen.append(sess.native_id)
    ni.already_imported_keys = lambda: set()
    try:
        # No limit: every session imported, newest-first.
        st = ni.JobStatus()
        ni._run_import(st, None, None)
        check(seen == expected_desc, f"order {seen} != {expected_desc}")
        check(st.imported == 5, f"imported {st.imported} != 5")
        check(st.status == "done", f"status {st.status}")

        # limit=2: keeps the two most recent only.
        seen.clear()
        st = ni.JobStatus()
        ni._run_import(st, None, 2)
        check(seen == ["newest", "second"], f"limited order {seen}")
        check(st.imported == 2, f"limited imported {st.imported} != 2")
    finally:
        ni.enumerate_native_sessions = orig_enum
        ni.import_session = orig_import
        ni.already_imported_keys = orig_keys

    print(f"OK — {CASES['n']} checks passed")


if __name__ == "__main__":
    main()
