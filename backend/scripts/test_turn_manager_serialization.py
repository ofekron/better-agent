"""Lock test: TurnManager serializes turns per session.

`has_active_turn(sid)` is True from the moment `run_turn` registers
its cancel_event until the finally block pops it. Two concurrent
turns on the same session would corrupt `active_run_ids`,
`current_assistant_msgs`, and the run_state registry (each is
overwritten not appended-merged on the second entry).

Coordinator's `_run_session_processor` enforces this by dequeuing
one prompt at a time, but TurnManager itself must EXPOSE the
serialization signal so the processor can correctly gate. This
test asserts the signals required for the processor's gating
behave as documented when state is mutated as `run_turn` does.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_tm_serial_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from turn_manager import TurnManager  # noqa: E402


class _StubCoordinator:
    def __init__(self) -> None:
        # has_active_runs() looks at these on the coordinator.
        self._in_flight_prompts: dict[str, int] = {}
        self._prompt_queues: dict = {}
        self._session_cancelled: dict[str, bool] = {}

    # cancel_turn() reaches for these statics.
    @staticmethod
    def _run_is_persistent(run_id: str) -> bool:  # noqa: ARG004
        return False

    @staticmethod
    def _cancel_turn_fanout(run_id: str) -> bool:  # noqa: ARG004
        return True


def test_has_active_turn_reflects_cancel_event_presence() -> None:
    tm = TurnManager(_StubCoordinator())
    sid = "sid-1"
    assert tm.has_active_turn(sid) is False
    # Mirror run_turn's registration sequence:
    tm.cancel_events[sid] = asyncio.Event()
    assert tm.has_active_turn(sid) is True
    # And the finally-pop:
    tm.cancel_events.pop(sid, None)
    assert tm.has_active_turn(sid) is False


def test_has_active_runs_three_signals() -> None:
    """has_active_runs must return True for the ENTIRE window between
    a prompt being enqueued and run_turn's finally cleaning up.
    Tested by setting each of the three signals in isolation."""
    tm = TurnManager(_StubCoordinator())
    sid = "sid-2"
    # No signals → False.
    assert tm.has_active_runs(sid) is False
    # Signal (3): active_run_ids set.
    tm.active_run_ids[sid] = ["run-1"]
    assert tm.has_active_runs(sid) is True
    tm.active_run_ids.pop(sid)
    # Signal (2): _in_flight_prompts > 0 (on Coordinator).
    tm._c._in_flight_prompts[sid] = 1
    assert tm.has_active_runs(sid) is True
    tm._c._in_flight_prompts.pop(sid)
    # Signal (1): queue.qsize() > 0 (on Coordinator).
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"prompt": "x"})
    tm._c._prompt_queues[sid] = q
    assert tm.has_active_runs(sid) is True


def test_cancel_event_set_breaks_serialized_concurrent_turn() -> None:
    """If a second `run_turn` tried to start while the first is in
    flight, it would overwrite `cancel_events[sid]` — silently
    detaching the first turn's cancel signal. This test asserts the
    detection signal (has_active_turn) is reliably True throughout
    the window, so a correct processor will NOT enter a second
    run_turn while the first holds the slot."""
    tm = TurnManager(_StubCoordinator())
    sid = "sid-3"

    async def _scenario() -> bool:
        ev = asyncio.Event()
        tm.cancel_events[sid] = ev
        in_flight_observed = tm.has_active_turn(sid)
        # Even after the cancel_event is SET (cancel requested but
        # the finally has not popped yet), has_active_turn stays True
        # — the slot is still owned. The processor must wait for the
        # finally cleanup before dequeuing the next prompt.
        ev.set()
        still_active = tm.has_active_turn(sid)
        tm.cancel_events.pop(sid, None)
        cleared = tm.has_active_turn(sid)
        return in_flight_observed and still_active and not cleared

    assert asyncio.run(_scenario()) is True


def test_cancel_turn_pops_interrupted_by_cross_ref() -> None:
    """cancel_turn stashes an interrupted-by lifecycle id so the
    in-flight turn's `user_message_done` carries the cross-ref. This
    state is turn-scoped and must live on TurnManager, not leak across
    sessions."""
    tm = TurnManager(_StubCoordinator())
    sid_a = "sid-a"
    sid_b = "sid-b"
    tm.cancel_events[sid_a] = asyncio.Event()
    tm.active_run_ids[sid_a] = ["run-A"]

    async def _go() -> None:
        ok = await tm.cancel_turn(sid_a, interrupted_by_msg_id="msg-x")
        assert ok is True

    asyncio.run(_go())
    assert tm._interrupted_by_msg_id.get(sid_a) == "msg-x"
    # Cross-session isolation.
    assert sid_b not in tm._interrupted_by_msg_id


if __name__ == "__main__":
    test_has_active_turn_reflects_cancel_event_presence()
    test_has_active_runs_three_signals()
    test_cancel_event_set_breaks_serialized_concurrent_turn()
    test_cancel_turn_pops_interrupted_by_cross_ref()
    print("OK: TurnManager serialization signals correct")
