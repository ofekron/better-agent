"""Regression test for `paths.atomic_replace` — the Windows-hardened
atomic rename used by every atomic write in the backend.

The bug it locks: on Windows, `os.replace` (MoveFileEx) intermittently
fails with ERROR_ACCESS_DENIED (WinError 5) or ERROR_SHARING_VIOLATION
(WinError 32) when antivirus / the Search indexer / a concurrent reader
briefly holds the destination open. That surfaced as
`PermissionError: [WinError 5] Access is denied` inside
`session_store.write_session_full`, losing the session write. POSIX never
hits this, so the helper must be a zero-behavior-change passthrough there.

Cross-platform: the transient-lock cases monkeypatch `os.replace`, so the
retry semantics are exercised on every OS, not just Windows.

Run with:
    cd backend && .venv/bin/python scripts/test_atomic_replace.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _win_err(code: int) -> OSError:
    e = OSError("simulated")
    e.winerror = code
    return e


def _happy_path(d: str) -> bool:
    src = os.path.join(d, "a.src")
    dst = os.path.join(d, "a.dst")
    Path(src).write_text("new", encoding="utf-8")
    Path(dst).write_text("old", encoding="utf-8")
    paths.atomic_replace(src, dst)
    return Path(dst).read_text(encoding="utf-8") == "new" and not os.path.exists(src)


def _transient_then_success(d: str) -> bool:
    src = os.path.join(d, "b.src")
    dst = os.path.join(d, "b.dst")
    Path(src).write_text("again", encoding="utf-8")
    Path(dst).write_text("old", encoding="utf-8")
    real = os.replace
    n = {"c": 0}

    def flaky(a, b):
        n["c"] += 1
        if n["c"] < 4:  # fail the first 3 attempts with a transient lock
            raise _win_err(5)
        return real(a, b)

    os.replace = flaky
    try:
        paths.atomic_replace(src, dst, _base_delay=0.001)
    finally:
        os.replace = real
    # On POSIX the helper short-circuits to a single real os.replace and the
    # monkeypatch is never consulted; only assert the retry count on Windows.
    if os.name == "nt" and n["c"] != 4:
        return False
    return Path(dst).read_text(encoding="utf-8") == "again"


def _non_transient_reraises_immediately() -> bool:
    if os.name != "nt":
        return True  # passthrough on POSIX never inspects winerror
    real = os.replace
    n = {"c": 0}

    def hard(a, b):
        n["c"] += 1
        raise _win_err(2)  # ERROR_FILE_NOT_FOUND — not a transient lock

    os.replace = hard
    try:
        paths.atomic_replace("x", "y", _base_delay=0.001)
        return False
    except OSError as e:
        return getattr(e, "winerror", None) == 2 and n["c"] == 1
    finally:
        os.replace = real


def _budget_exhaustion_reraises() -> bool:
    if os.name != "nt":
        return True
    real = os.replace
    n = {"c": 0}

    def always(a, b):
        n["c"] += 1
        raise _win_err(32)  # ERROR_SHARING_VIOLATION, never clears

    os.replace = always
    try:
        paths.atomic_replace("x", "y", _retries=3, _base_delay=0.001)
        return False
    except OSError as e:
        return getattr(e, "winerror", None) == 32 and n["c"] == 3
    finally:
        os.replace = real


def main() -> int:
    d = tempfile.mkdtemp(prefix="bc-test-atomic-replace-")
    try:
        ok = (
            _happy_path(d)
            and _transient_then_success(d)
            and _non_transient_reraises_immediately()
            and _budget_exhaustion_reraises()
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print(f"{PASS if ok else FAIL} atomic_replace retries transient Windows locks, "
          f"passes through on POSIX, re-raises non-transient errors")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
