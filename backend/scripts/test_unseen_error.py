"""Locks the per-session unseen-error attention dot semantics:

1. `set_unseen_error` flips `has_unseen_error` to True and fires
   `error_changed{has_error: True}`.
2. Change-gate: re-setting the same text does NOT fire again.
3. `clear_unseen_error` flips back to False and fires
   `error_changed{has_error: False}`; clearing when already clear is a no-op.
4. Lifecycle: the dot is retired ONLY when the session resumes work
   (`clear_unseen_error`, called at turn-start). It is decoupled from
   view/seen state — `mark_seen` must NOT clear it.
5. Persistence: the `unseen_error` field survives a backend "restart"
   (drop the in-memory roots, re-hydrate from disk).

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

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_session() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-unseen-error",
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


def _mk_session_with_assistant() -> str:
    """A session that already has an assistant message on disk (the shape
    a real turn leaves behind), so derivation off the last message works."""
    sid = _mk_session()
    session_manager.append_user_msg(sid, {"id": "u1", "role": "user", "content": "go"})
    session_manager.append_assistant_msg(sid, {
        "id": "a1", "role": "assistant", "content": "", "events": [],
    })
    return sid


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


def test_mark_seen_does_not_clear_error() -> None:
    """The error dot is decoupled from view/seen state. Acking the session
    (mark_seen) must leave the dot in place — it retires only when the
    session resumes work (turn-start → clear_unseen_error)."""
    sid = _mk_session()
    session_manager.set_unseen_error(sid, "fail")
    assert session_manager.has_unseen_error(sid) is True

    fires, listener = _capture_fires()
    session_manager.mark_seen(sid, None)
    assert session_manager.has_unseen_error(sid) is True, (
        "mark_seen must NOT retire the unseen-error dot"
    )
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert err_fires == [], f"mark_seen fired error_changed: {err_fires}"

    session_manager._listeners.remove(listener)
    print(f"{PASS} mark_seen_does_not_clear_error")


def test_clear_retires_dot() -> None:
    """The turn-start hook (clear_unseen_error) is what retires the dot."""
    sid = _mk_session()
    session_manager.set_unseen_error(sid, "fail")
    assert session_manager.has_unseen_error(sid) is True

    fires, listener = _capture_fires()
    session_manager.clear_unseen_error(sid)
    assert session_manager.has_unseen_error(sid) is False, (
        "clear_unseen_error must retire the dot"
    )
    err_fires = [f for f in fires if f.get("kind") == "error_changed"]
    assert len(err_fires) == 1 and err_fires[0]["has_error"] is False, err_fires

    session_manager._listeners.remove(listener)
    print(f"{PASS} clear_retires_dot")


def test_derived_from_last_assistant_error() -> None:
    """The dot is ALSO derived from the last assistant message's error
    state — the durable source of truth. Covers sessions that errored via
    a path the flag missed (run-recovery, pre-feature errors), so they
    show the dot on the next snapshot without a new finalize."""
    sid = _mk_session_with_assistant()
    assert session_manager.has_unseen_error(sid) is False, (
        "fresh non-error assistant message → no dot"
    )

    # The recovery / existing-session path: the message is errored but
    # the flag was never set.
    session_manager.set_assistant_error(sid, "a1", "HTTP 400: bad request")
    assert session_manager.has_unseen_error(sid) is True, (
        "errored last assistant message must show the dot via derivation"
    )

    # A new turn appends a fresh non-error assistant message → it becomes
    # the last → derivation clears the dot (mirrors turn-start).
    session_manager.append_assistant_msg(sid, {
        "id": "a2", "role": "assistant", "content": "", "events": [],
    })
    assert session_manager.has_unseen_error(sid) is False, (
        "new non-error last assistant message must clear the derived dot"
    )
    print(f"{PASS} derived_from_last_assistant_error")


def test_stale_flag_does_not_override_newer_success() -> None:
    sid = _mk_session_with_assistant()
    session_manager.set_assistant_error(sid, "a1", "HTTP 400: bad request")
    assert session_manager.has_unseen_error(sid) is True

    session_manager.append_user_msg(sid, {"id": "u2", "role": "user", "content": "again"})
    session_manager.append_assistant_msg(sid, {
        "id": "a2", "role": "assistant", "content": "ok", "events": [],
    })
    session_manager.set_unseen_error(sid, "stale old failure")

    assert session_manager.has_unseen_error(sid) is False, (
        "stale unseen_error must not mark a session after a newer successful turn"
    )
    summary = next(s for s in session_store.list_sessions() if s["id"] == sid)
    assert summary.get("unseen_error") is None, (
        "sidebar summary must project only the latest assistant turn failure"
    )
    print(f"{PASS} stale_flag_does_not_override_newer_success")


def test_exception_path_error_with_no_surviving_assistant_msg() -> None:
    """Mirrors `_finalize_turn_messages`'s exception path exactly:
    `mark_user_error` stamps the user msg, `remove_assistant_msg` deletes
    the lazily-created assistant msg (so the last message is the errored
    user msg, not an assistant msg from an earlier successful turn), then
    `set_unseen_error` fires. The dot — and the sidebar summary text —
    must still surface the failure via the user message's `errorText`,
    not silently disappear because an OLDER assistant message succeeded."""
    sid = _mk_session_with_assistant()
    assert session_manager.has_unseen_error(sid) is False

    session_manager.append_user_msg(sid, {"id": "u2", "role": "user", "content": "again"})
    session_manager.append_assistant_msg(sid, {
        "id": "a2", "role": "assistant", "content": "", "events": [],
    })
    session_manager.mark_user_error(sid, "u2", "HTTP 500: boom")
    session_manager.remove_assistant_msg(sid, "a2")
    session_manager.set_unseen_error(sid, "HTTP 500: boom")

    assert session_manager.has_unseen_error(sid) is True, (
        "exception-path error with no surviving assistant msg must still show the dot"
    )
    session_manager.flush_pending_persists()
    summary = next(s for s in session_store.list_sessions() if s["id"] == sid)
    assert summary.get("unseen_error") == "HTTP 500: boom", (
        f"sidebar summary must surface the exception-path error text, got {summary.get('unseen_error')!r}"
    )
    print(f"{PASS} exception_path_error_with_no_surviving_assistant_msg")


def test_derived_error_uses_error_text_not_bool() -> None:
    """`current_turn_error` must project the human-readable `errorText`,
    not `str(True)`, when deriving from an assistant message's error."""
    sid = _mk_session_with_assistant()
    session_manager.set_assistant_error(sid, "a1", "HTTP 400: bad request")
    session_manager.flush_pending_persists()
    summary = next(s for s in session_store.list_sessions() if s["id"] == sid)
    assert summary.get("unseen_error") == "HTTP 400: bad request", (
        f"expected the assistant error text, got {summary.get('unseen_error')!r}"
    )
    print(f"{PASS} derived_error_uses_error_text_not_bool")


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
        test_mark_seen_does_not_clear_error()
        test_clear_retires_dot()
        test_derived_from_last_assistant_error()
        test_stale_flag_does_not_override_newer_success()
        test_exception_path_error_with_no_surviving_assistant_msg()
        test_derived_error_uses_error_text_not_bool()
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
