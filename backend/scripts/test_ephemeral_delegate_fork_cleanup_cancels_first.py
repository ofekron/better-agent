"""Regression test: `_cleanup_ephemeral_delegate_fork` must cancel the
fork's active run before deleting it.

Iteration-74 finding: this is a THIRD, independent orphaned-delegate-fork
deletion path (distinct from the two fixed earlier the same day — commits
29d01d58e and 4d003508a). It fires at the end of `run_delegation_locked`,
right after the delegation's own terminal lifecycle event is published —
but the ephemeral fork's own underlying turn may not actually be finished
yet. With no cancellation, `session_manager.delete` orphaned the fork's
runner process exactly like the other two paths: confirmed live with 204
repeated "provider stream journal publish failed" / "cannot resolve root
for session" errors for one fork — the largest occurrence count of the
three variants.

Run with:
    cd backend && .venv/bin/python scripts/test_ephemeral_delegate_fork_cleanup_cancels_first.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_ephemeral_fork_cleanup_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


async def _test_cleanup_cancels_before_delete(failures: list[str]) -> None:
    from orchs.manager._delegation import _cleanup_ephemeral_delegate_fork
    from session_manager import manager as session_manager

    worker = session_manager.create(
        name="worker", cwd=_TMP, orchestration_mode="native",
    )
    session_manager.set_agent_sid(worker["id"], "native", str(uuid.uuid4()))
    fork = session_manager.fork(worker["id"], name="ephemeral-fork")

    cancelled: list[str] = []
    order: list[str] = []

    class _FakeCoordinator:
        async def cancel_session(self, session_id: str) -> int:
            cancelled.append(session_id)
            order.append("cancel")
            return 0

    await _cleanup_ephemeral_delegate_fork(
        coordinator=_FakeCoordinator(),
        app_session_id=worker["id"],
        worker_agent_session_id=worker["id"],
        fork_agent_session_id=fork["id"],
        fork_agent_sid=None,
        ephemeral=True,
    )
    if session_manager.get(fork["id"]) is None:
        order.append("delete")

    _check(
        cancelled == [fork["id"]],
        "cancel_session was called for the ephemeral fork",
        failures,
    )
    _check(
        order == ["cancel", "delete"],
        "cancel happened before the fork was deleted",
        failures,
    )
    _check(
        session_manager.get(fork["id"]) is None,
        "the ephemeral fork was still deleted",
        failures,
    )


async def _test_non_ephemeral_skips_cleanup(failures: list[str]) -> None:
    from orchs.manager._delegation import _cleanup_ephemeral_delegate_fork
    from session_manager import manager as session_manager

    worker = session_manager.create(
        name="worker2", cwd=_TMP, orchestration_mode="native",
    )
    session_manager.set_agent_sid(worker["id"], "native", str(uuid.uuid4()))
    fork = session_manager.fork(worker["id"], name="persistent-fork")

    cancelled: list[str] = []

    class _FakeCoordinator:
        async def cancel_session(self, session_id: str) -> int:
            cancelled.append(session_id)
            return 0

    await _cleanup_ephemeral_delegate_fork(
        coordinator=_FakeCoordinator(),
        app_session_id=worker["id"],
        worker_agent_session_id=worker["id"],
        fork_agent_session_id=fork["id"],
        fork_agent_sid=None,
        ephemeral=False,
    )

    _check(cancelled == [], "non-ephemeral fork is left untouched", failures)
    _check(
        session_manager.get(fork["id"]) is not None,
        "non-ephemeral fork is not deleted",
        failures,
    )


def main_() -> int:
    failures: list[str] = []
    print("ephemeral delegate fork cleanup -> cancels before delete")
    asyncio.run(_test_cleanup_cancels_before_delete(failures))
    print("non-ephemeral fork -> cleanup is a no-op")
    asyncio.run(_test_non_ephemeral_skips_cleanup(failures))
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
