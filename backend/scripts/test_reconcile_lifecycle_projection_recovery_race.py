"""Regression: the periodic lifecycle-reconcile tick must not race
startup recovery and blank the running indicator for a session that is
genuinely still alive but whose recovery integration hasn't finished yet.

Background: `reconcile_lifecycle_projection` (turn_manager.py) is the only
code path that clears a `lifecycle.turn.state` stuck "running" when no
live run backs it. It now runs on the recurring 2s background tick (see
test_turn_manager_lifecycle_emit.py's orphan-clears test), not just once
at startup.

At boot, `_recover_in_flight_task` (recovery.py) restores each recovered
session's lifecycle projection (turn.state="running") from disk BEFORE
it finishes re-registering that session's live run/pid into
`turn_manager._run_state`/`active_run_ids` — `register_session_recovery`
(startup_recovery_gate.py) marks the session pending, and only
`mark_session_recovery_done` (called after `integrate_recovered_runs`
completes for that session's batch) clears it. The ORIGINAL one-shot
`reconcile_lifecycle_projection` call in recovery.py runs after every
batch is integrated, so this window never mattered before. The periodic
tick has no such ordering guarantee: if it fires while a session is still
`startup_recovery_gate.is_session_pending(sid)`, `live_run_ids` for that
session is empty NOT because the run is dead, but because recovery
hasn't written it yet — reconcile must not treat that as "orphaned" and
force the running indicator off for a session that is, in reality, still
alive and mid-recovery.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home
_test_home.isolate("bc_test_tm_recovery_race_")

import startup_recovery_gate  # noqa: E402
from turn_manager import TurnManager  # noqa: E402


class _StubCoordinator:
    """Minimal Coordinator stub — mirrors the one in
    test_turn_manager_lifecycle_emit.py. `reconcile_lifecycle_projection`
    reaches only `session_manager` (module global) and
    `startup_recovery_gate`, not Coordinator, so an empty stub suffices."""


_IDENTITY = {
    "user_turn_id": "user-turn",
    "execution_turn_id": "execution-turn",
    "lifecycle_message_id": "lifecycle-message",
    "assistant_message_id": "assistant-message",
}


def test_reconcile_lifecycle_projection_never_forces_off_a_session_still_pending_recovery() -> None:
    """Reproduces the regression: a session mid-recovery (registered
    pending, not yet integrated) must keep its running indicator through
    a reconcile pass that lands in that window."""
    sid = "sid-recovery-race-pending"

    async def _scenario() -> None:
        tm = TurnManager(_StubCoordinator())
        # Simulate the restored-from-disk lifecycle projection: turn.state
        # is "running" the moment the session's recovery record loads,
        # well before its run/pid is re-registered in turn_manager.
        await tm.lifecycle.publish(
            "lifecycle.turn_start",
            root_id=sid,
            session_id=sid,
            payload=_IDENTITY,
        )
        assert tm.lifecycle.has_active_session(sid) is True

        # Recovery has classified this session as alive and registered it
        # pending — but `integrate_recovered_runs` for its batch hasn't
        # run yet, so `_run_state`/`active_run_ids` are still empty for it.
        startup_recovery_gate.begin_recovery()
        startup_recovery_gate.register_session_recovery({sid})
        assert tm._run_state.get(sid) is None
        try:
            assert startup_recovery_gate.is_session_pending(sid) is True

            # The periodic 2s tick lands in this exact window.
            await tm.reconcile_lifecycle_projection()

            assert tm.lifecycle.has_active_session(sid) is True, (
                "reconcile_lifecycle_projection forced a session's running "
                "indicator off while its recovery integration was still "
                "pending — the session is genuinely still alive, its run "
                "just hasn't been re-registered into turn_manager yet"
            )
        finally:
            startup_recovery_gate.reset_for_tests()

    asyncio.run(_scenario())


def test_reconcile_lifecycle_projection_clears_orphan_once_recovery_marks_it_done() -> None:
    """Once recovery finishes integrating a session (or determines it did
    NOT survive) and calls `mark_session_recovery_done`, reconcile must
    resume normal behavior: a session with no live run backing it clears,
    same as the non-recovery orphan case."""
    sid = "sid-recovery-race-done"

    async def _scenario() -> None:
        tm = TurnManager(_StubCoordinator())
        await tm.lifecycle.publish(
            "lifecycle.turn_start",
            root_id=sid,
            session_id=sid,
            payload=_IDENTITY,
        )
        startup_recovery_gate.begin_recovery()
        startup_recovery_gate.register_session_recovery({sid})
        try:
            # Recovery determined this session did NOT come back alive
            # (or finished integrating it) and released the gate.
            startup_recovery_gate.mark_session_recovery_done(sid)
            assert startup_recovery_gate.is_session_pending(sid) is False

            await tm.reconcile_lifecycle_projection()

            assert tm.lifecycle.has_active_session(sid) is False, (
                "reconcile_lifecycle_projection must still clear a "
                "genuinely orphaned session once recovery has released it"
            )
        finally:
            startup_recovery_gate.reset_for_tests()

    asyncio.run(_scenario())


if __name__ == "__main__":
    test_reconcile_lifecycle_projection_never_forces_off_a_session_still_pending_recovery()
    test_reconcile_lifecycle_projection_clears_orphan_once_recovery_marks_it_done()
    print("OK: reconcile_lifecycle_projection respects the recovery gate")
