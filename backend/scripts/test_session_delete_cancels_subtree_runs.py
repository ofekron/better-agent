"""Deleting a session cancels active runs for its ENTIRE subtree, not
just the top session and its `working_mode` children.

Live incident (2026-07-31): `_delete_session_tree` called
`coordinator.cancel_session` for the deleted session id and, separately,
for each `working_mode` child — but delegation/team-orchestration forks
live in the same tree without being `working_mode` sessions, so they
were never cancelled. `session_manager.subtree_ids` (used later purely
for disk/tab cleanup) already returns the full descendant set; a live
fork whose root got deleted kept its provider-cli runner process alive
and working for 17+ minutes afterward, spamming
"cannot resolve root for session" errors in turn_manager on every
streamed event because its root had vanished out from under it.

Fix: cancel every id in `removed_sids` (the full subtree) before
`session_manager.delete` runs. `cancel_session` is idempotent, so
re-cancelling the top id / working-mode children already handled above
is a harmless no-op.

Run with:
    cd backend && .venv/bin/python scripts/test_session_delete_cancels_subtree_runs.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_delete_subtree_cancel_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


async def _test_delete_cancels_fork_not_just_working_mode(failures: list[str]) -> None:
    import main

    sm = main.session_manager
    sm.bind_loop(asyncio.get_running_loop())

    cancelled: list[str] = []

    async def _spy_cancel_session(app_session_id: str) -> int:
        cancelled.append(app_session_id)
        return 0

    orig_cancel = main.coordinator.cancel_session
    main.coordinator.cancel_session = _spy_cancel_session
    try:
        root = sm.create(name="root", cwd=_TMP, orchestration_mode="native")
        # A fork is a subtree descendant that is NOT a `working_mode`
        # session — exactly the category the working-mode-only cascade
        # missed. Forking requires the parent to have a native agent
        # session to branch from.
        sm.set_agent_sid(root["id"], "native", str(uuid.uuid4()))
        fork = sm.fork(root["id"], name="root-fork")

        ok = await main._delete_session_tree(root["id"])
        _check(ok, "_delete_session_tree reports success", failures)
        _check(
            root["id"] in cancelled,
            "cancel_session was called for the deleted root",
            failures,
        )
        _check(
            fork["id"] in cancelled,
            "cancel_session was called for the fork descendant "
            "(previously orphaned: not a working_mode child)",
            failures,
        )
    finally:
        main.coordinator.cancel_session = orig_cancel


def main_() -> int:
    failures: list[str] = []
    print("session delete -> cancels the full subtree, including non-working_mode forks")
    asyncio.run(_test_delete_cancels_fork_not_just_working_mode(failures))
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
