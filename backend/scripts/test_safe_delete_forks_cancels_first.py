"""Regression test: `_safe_delete_forks` must cancel each fork's active
run before deleting it.

Iteration-80 finding: this is a FOURTH, independent orphaned-delegate-
fork deletion path (distinct from the three fixed earlier the same day
- commits 29d01d58e, 4d003508a, d2b2d64ab). It's a shared helper called
from 6 sites across orchs/manager/_rewind.py (5 call sites inside
rewind_workers_for_turn) and orchs/manager/_delegation.py (1 call site
in run_delegation's stale-worker-registry cleanup). None of the 6
cancelled a fork's active run before deleting it. Confirmed live with
141 repeated "cannot resolve root for session" errors for one fork,
recurring after all 3 earlier fixes had been clean for 5+ hours.

Run with:
    cd backend && .venv/bin/python scripts/test_safe_delete_forks_cancels_first.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_safe_delete_forks_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


async def _test_cancels_each_fork_before_delete(failures: list[str]) -> None:
    from orchs.manager._rewind import _safe_delete_forks
    from session_manager import manager as session_manager

    worker = session_manager.create(
        name="worker", cwd=_TMP, orchestration_mode="native",
    )
    session_manager.set_agent_sid(worker["id"], "native", str(uuid.uuid4()))
    fork_a = session_manager.fork(worker["id"], name="fork-a")
    fork_b = session_manager.fork(worker["id"], name="fork-b")

    cancelled: list[str] = []
    still_existed_at_cancel_time: list[bool] = []

    class _FakeCoordinator:
        async def cancel_session(self, session_id: str) -> int:
            cancelled.append(session_id)
            # If cancel ran AFTER delete, the session would already be
            # gone here — proves cancel happens while the fork is still
            # live, not as an afterthought post-delete.
            still_existed_at_cancel_time.append(
                session_manager.get(session_id) is not None
            )
            return 0

    await _safe_delete_forks(
        _FakeCoordinator(),
        [fork_a["id"], fork_b["id"]],
        "delete delegate-fork BC %s failed",
    )

    _check(
        set(cancelled) == {fork_a["id"], fork_b["id"]},
        "cancel_session was called for every fork",
        failures,
    )
    _check(
        still_existed_at_cancel_time == [True, True],
        "each fork still existed when cancel_session ran (cancel before delete)",
        failures,
    )
    _check(
        session_manager.get(fork_a["id"]) is None
        and session_manager.get(fork_b["id"]) is None,
        "both forks were still deleted",
        failures,
    )


async def _test_cancel_failure_does_not_block_delete(failures: list[str]) -> None:
    from orchs.manager._rewind import _safe_delete_forks
    from session_manager import manager as session_manager

    worker = session_manager.create(
        name="worker2", cwd=_TMP, orchestration_mode="native",
    )
    session_manager.set_agent_sid(worker["id"], "native", str(uuid.uuid4()))
    fork = session_manager.fork(worker["id"], name="fork-fails-cancel")

    class _FailingCoordinator:
        async def cancel_session(self, session_id: str) -> int:
            raise RuntimeError("boom")

    await _safe_delete_forks(
        _FailingCoordinator(),
        [fork["id"]],
        "delete delegate-fork BC %s failed",
    )

    _check(
        session_manager.get(fork["id"]) is None,
        "fork is still deleted even if cancel_session raises "
        "(INVARIANT: cleanup must never raise/block on cancel failure)",
        failures,
    )


def main_() -> int:
    failures: list[str] = []
    print("_safe_delete_forks -> cancels each fork before deleting it")
    asyncio.run(_test_cancels_each_fork_before_delete(failures))
    print("_safe_delete_forks -> cancel failure does not block delete")
    asyncio.run(_test_cancel_failure_does_not_block_delete(failures))
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
