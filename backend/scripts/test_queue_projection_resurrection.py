"""Regression: a deleted session must not resurrect across a restart.

The queue recovery projection can retain a stale row for a deleted session
(a crash window between root removal and the durable projection DELETE, or a
fingerprint certified while a stale row sat in the sqlite). On the next
startup the verbatim sqlite load re-surfaced that row and
`_re_enqueue_queued_prompts` blindly re-submitted it via
`coordinator.submit_prompt_async`, materializing a brand-new root for the
dead sid.

Two independent guards now stop this:
  1. `_re_enqueue_queued_prompts` checks `session_manager.is_live_session(sid)`
     before re-submitting and drops the stale row.
  2. The projection load path self-heals, dropping any sid with no live root
     or a deletion tombstone.

These tests fail before either guard and pass after. They build projection
records directly (the queue-recovery shape) so they exercise the guards, not
session_manager's admission internals.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

import asyncio
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("ba-test-queue-resurrection-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import recovery  # noqa: E402
import session_queue_projection  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _Coordinator:
    """Captures submits without spawning a real provider run."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def is_prompt_item_in_flight(self, sid: str, item_id: str) -> bool:
        return False

    async def submit_prompt_async(self, sid: str, params: dict, **_kwargs) -> str:
        self.submitted.append((sid, params))
        return params.get("_queued_id") or "queued-runtime-id"


def _record(sid: str, *, queued: bool = True) -> dict:
    return {
        "id": sid,
        "model": "sonnet",
        "cwd": "/tmp/resurrection",
        "queued_prompts": [
            {
                "id": "qp-resurrect",
                "client_id": "client-resurrect",
                "content": "do not resurrect me",
                "orchestration_mode": "native",
            }
        ] if queued else [],
        "user_message_acks": {},
        "user_lifecycle_msg_ids": [],
    }


def _make_live_session() -> str:
    sess = session_manager.create(
        name="resurrection-guard",
        model="sonnet",
        cwd="/tmp/resurrection",
        orchestration_mode="native",
        source="cli",
    )
    return sess["id"]


def _session_file_exists(sid: str) -> bool:
    return os.path.exists(session_store.session_file_path(sid))


def _reset_projection_memory() -> None:
    """Force the next read to reload from disk (simulate a process restart)."""
    session_manager.flush_pending_persists()
    session_queue_projection.shutdown(timeout=5.0)


def _certify_current() -> None:
    """Make `projection_is_current()` True so the load (not rebuild) path runs."""
    session_queue_projection.shutdown(timeout=5.0, certify=True)


async def _run_reenqueue(stale_records: list[dict]) -> _Coordinator:
    """Drive `_re_enqueue_queued_prompts` with a fixed projection view and a
    fake coordinator. `ensure_current_or_rebuild` is stubbed so no real load
    runs against the home; `session_manager` stays real so `is_live_session`
    is authoritative."""
    coordinator = _Coordinator()
    original_coordinator = recovery._coordinator_ref
    original_list = session_queue_projection.list_queued_records
    original_ensure = session_queue_projection.ensure_current_or_rebuild
    recovery._coordinator_ref = coordinator
    session_queue_projection.list_queued_records = (
        lambda **_kwargs: list(stale_records)
    )
    session_queue_projection.ensure_current_or_rebuild = lambda **_kwargs: False
    try:
        await recovery._re_enqueue_queued_prompts()
    finally:
        session_queue_projection.ensure_current_or_rebuild = original_ensure
        session_queue_projection.list_queued_records = original_list
        recovery._coordinator_ref = original_coordinator
    return coordinator


async def test_replay_skips_tombstoned_session() -> bool:
    """T1 / guard #1: a stale projection row for a tombstoned sid must not be
    re-submitted. Feeds the stale record straight through, exactly as the
    buggy verbatim load would have surfaced it."""
    sid = _make_live_session()
    assert session_manager.is_live_session(sid)

    # Delete: file removed, tombstone written. is_live_session now False.
    assert session_manager.delete(sid) is True
    assert session_manager.is_live_session(sid) is False
    assert not _session_file_exists(sid)

    coordinator = await _run_reenqueue([_record(sid, queued=True)])

    submitted_ok = coordinator.submitted == []
    row_dropped = session_queue_projection.get(sid) is None
    no_file = not _session_file_exists(sid)
    ok = submitted_ok and row_dropped and no_file
    print(
        f"{PASS if ok else FAIL} re-enqueue skips tombstoned session "
        f"(submitted={len(coordinator.submitted)}, row_dropped={row_dropped})"
    )
    return ok


async def test_projection_load_self_heals_tombstoned_row() -> bool:
    """T1b / guard #2: the verbatim sqlite load drops a tombstoned stale row.
    Seeds the stale row back into the sqlite, certifies the fingerprint so the
    load path (not rebuild) runs, then reloads — the row must vanish."""
    sid = _make_live_session()
    assert session_manager.delete(sid) is True
    assert session_manager.is_live_session(sid) is False

    # Re-insert the stale row directly (the crash-window condition).
    session_queue_projection.upsert_record(_record(sid, queued=True))
    assert session_queue_projection.get(sid) is not None

    _certify_current()
    _reset_projection_memory()
    rebuilt = session_queue_projection.ensure_current_or_rebuild()
    assert rebuilt is False, "load path must have been taken (projection current)"

    ok = session_queue_projection.get(sid) is None
    print(f"{PASS if ok else FAIL} projection load self-heals tombstoned row")
    return ok


async def test_delete_invalidates_projection_across_restart() -> bool:
    """T2: after delete, a simulated restart leaves no projection row and no
    live session."""
    sid = _make_live_session()
    session_queue_projection.upsert_record(_record(sid, queued=True))
    assert session_queue_projection.get(sid) is not None

    assert session_manager.delete(sid) is True

    _reset_projection_memory()
    _certify_current()
    session_queue_projection.ensure_current_or_rebuild()

    ok = (
        session_queue_projection.get(sid) is None
        and not session_manager.is_live_session(sid)
        and not _session_file_exists(sid)
    )
    print(f"{PASS if ok else FAIL} delete invalidates projection across restart")
    return ok


async def test_live_session_survives_restart_without_duplication() -> bool:
    """T3 / no-false-positives: a live queued session is re-submitted exactly
    once, and repeated simulated restarts never duplicate on-disk files."""
    sid = _make_live_session()
    assert session_manager.is_live_session(sid)

    coordinator = await _run_reenqueue([_record(sid, queued=True)])
    submitted_once = len(coordinator.submitted) == 1
    still_live = session_manager.is_live_session(sid)

    before = len(list(session_store._session_json_files()))
    for _ in range(3):
        _reset_projection_memory()
        _certify_current()
        session_queue_projection.ensure_current_or_rebuild()
    after = len(list(session_store._session_json_files()))

    session_manager.delete(sid)

    ok = submitted_once and still_live and before == after
    print(
        f"{PASS if ok else FAIL} live session survives restart without duplication "
        f"(submitted={len(coordinator.submitted)}, files {before}->{after})"
    )
    return ok


async def _run() -> bool:
    results = [
        await test_replay_skips_tombstoned_session(),
        await test_projection_load_self_heals_tombstoned_row(),
        await test_delete_invalidates_projection_across_restart(),
        await test_live_session_survives_restart_without_duplication(),
    ]
    print(f"\n{sum(1 for r in results if r)}/{len(results)} passed")
    return all(results)


def main_test() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        try:
            session_queue_projection.flush_pending_writes(timeout=5.0)
        except Exception:
            pass
        session_queue_projection.shutdown(timeout=5.0)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_test())
