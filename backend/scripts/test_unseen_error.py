"""Locks the per-session unseen-error attention dot semantics:

1. `set_unseen_error` flips `has_unseen_error` to True and fires
   `error_changed{has_error: True}`.
2. Change-gate: re-setting the same text does NOT fire again.
3. `clear_unseen_error` flips back to False and fires
   `error_changed{has_error: False}`; clearing when already clear is a no-op.
4. `mark_seen` (view-ack) retires the dot, mirroring how it zeroes unread.
5. Persistence: the `unseen_error` field survives a backend "restart"
   (drop the manager singleton, re-import).

Run with:
    cd backend && .venv/bin/python scripts/test_unseen_error.py
"""

from __future__ import annotations

import os
import shutil
import sys
import warnings

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-unseen-error-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_session() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-unseen-error",
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


def _capture_fires() -> tuple[list[dict], object]:
    events: list[dict] = []

    def listener(sid: str, change: dict) -> None:
        events.append({"sid": sid, **change})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(listener)
    return events, listener


def test_set_fires_and_flags() -> None:
    sid = _mk_session()
    fires, listener = _capture_fires()
    assert session_manager.has_unseen_error(sid) is False, "fresh session must not error"

    session_manager.set_unseen_error(sid, "boom: traceback")

    assert session_manager.has_unseen_error(sid) is True, "set did not flag"
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert len(err_fires) == 1, f"expected 1 fire, got {len(err_fires)}: {err_fires}"
    assert err_fires[0]["has_error"] is True
    assert err_fires[0]["error"] == "boom: traceback"

    # Change-gate: same text must NOT fire again.
    n_before = len(err_fires)
    session_manager.set_unseen_error(sid, "boom: traceback")
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert len(err_fires) == n_before, "change-gate violated on re-set"

    session_manager._listeners.remove(listener)
    print(f"{PASS} set_fires_and_flags")


def test_clear_fires_and_unflags() -> None:
    sid = _mk_session()
    session_manager.set_unseen_error(sid, "fail")
    assert session_manager.has_unseen_error(sid) is True

    fires, listener = _capture_fires()
    session_manager.clear_unseen_error(sid)
    assert session_manager.has_unseen_error(sid) is False, "clear did not unflag"
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert len(err_fires) == 1 and err_fires[0]["has_error"] is False, err_fires

    # No-op clear when nothing set must NOT fire.
    session_manager.clear_unseen_error(sid)
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert len(err_fires) == 1, "no-op clear fired"

    session_manager._listeners.remove(listener)
    print(f"{PASS} clear_fires_and_unflags")


def test_mark_seen_clears_error() -> None:
    sid = _mk_session()
    session_manager.set_unseen_error(sid, "fail")
    assert session_manager.has_unseen_error(sid) is True

    fires, listener = _capture_fires()
    session_manager.mark_seen(sid, None)
    assert session_manager.has_unseen_error(sid) is False, (
        "mark_seen must retire the unseen-error dot"
    )
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert len(err_fires) == 1 and err_fires[0]["has_error"] is False, err_fires

    session_manager._listeners.remove(listener)
    print(f"{PASS} mark_seen_clears_error")


def test_persistence_across_reload() -> None:
    sid = _mk_session()
    session_manager.set_unseen_error(sid, "persisted boom")
    assert session_manager.has_unseen_error(sid) is True

    # Drop in-memory state so the next read re-loads the session off disk.
    session_manager._roots.clear()

    assert session_manager.has_unseen_error(sid) is True, (
        "unseen_error must survive a manager reload (persisted on record)"
    )
    print(f"{PASS} persistence_across_reload")


def main() -> int:
    try:
        test_set_fires_and_flags()
        test_clear_fires_and_unflags()
        test_mark_seen_clears_error()
        test_persistence_across_reload()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
