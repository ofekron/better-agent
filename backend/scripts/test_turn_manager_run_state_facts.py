"""Failing-first coverage: TurnManager publishes `run.state.*` facts at
every `_run_state` mutation point (event-driven projection source for
`RunsSurfaceAdapter` — backend/adapters/runs_adapter.py).

Every test here failed before this pass: `_publish_run_state_fact` did not
exist, and none of `run_state_add`/`run_state_remove`/`run_state_set_pid`/
`run_state_clear_pid`/`run_state_mark_retrying`/`_run_state_set_target`/
`run_state_mark_provider_submitted`/`run_state_record_activity`/
`_update_startup_stalls`/`_prune_dead_entries` published anything.

Two testing strategies, used where each fits best:
  - A spy on `TurnManager._publish_run_state_fact` itself (mirrors
    `test_turn_manager_lifecycle_emit.py`'s `bus.publish` spy) — fast,
    deterministic, proves "this mutation calls the publish helper with
    these exact fields" without needing real cross-thread bus delivery.
  - One end-to-end test with a REAL bus subscriber (`bind_current_loop`)
    proving `_publish_run_state_fact` actually reaches `bus.
    publish_threadsafe` and a live subscriber receives it — the spy tests
    alone would not catch a helper that silently no-ops.

Isolated via `_test_home.isolate` before any backend import — same idiom
as test_turn_manager_lifecycle_emit.py.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_turn_manager_run_state_facts.py -q
    PYTHONPATH=. python3 backend/scripts/test_turn_manager_run_state_facts.py   # __main__ fallback
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home
_test_home.isolate("bc_test_tm_run_state_facts_")

from event_bus import BusEvent, bus  # noqa: E402
from turn_manager import TurnManager  # noqa: E402


class _StubCoordinator:
    """Minimal Coordinator stub — every `_run_state` mutator under test
    reaches only `session_manager` (module global) and `self._run_state`/
    `self.active_run_ids`/`self.cancel_events`, none of which need a real
    Coordinator."""


def _tm() -> TurnManager:
    return TurnManager(_StubCoordinator())


def _spy(tm: TurnManager) -> list[tuple[str, str, str, dict]]:
    """Replaces `tm._publish_run_state_fact` with a recording wrapper that
    still calls through (so the real `publish_threadsafe` call site stays
    exercised, not bypassed)."""
    captured: list[tuple[str, str, str, dict]] = []
    real = tm._publish_run_state_fact

    def _recording(fact_type: str, app_session_id: str, run_id: str, **fields) -> None:
        captured.append((fact_type, app_session_id, run_id, fields))
        real(fact_type, app_session_id, run_id, **fields)

    tm._publish_run_state_fact = _recording  # type: ignore[method-assign]
    return captured


# ---------------------------------------------------------------------------
# run_state_add — "run.state.anchor_changed"
# ---------------------------------------------------------------------------

def test_run_state_add_new_entry_publishes_anchor_changed() -> None:
    tm = _tm()
    captured = _spy(tm)
    tm.run_state_add(
        "sid-1", run_id="run-1", kind="native",
        target_message_id="msg-1", delegation_id=None, pid=123,
    )
    facts = [c for c in captured if c[0] == "run.state.anchor_changed"]
    assert len(facts) == 1, captured
    _, sid, run_id, fields = facts[0]
    assert sid == "sid-1" and run_id == "run-1"
    assert fields["kind"] == "native"
    assert fields["target_message_id"] == "msg-1"
    assert fields["pid"] == 123


def test_run_state_add_existing_entry_update_publishes_anchor_changed() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=None)
    captured = _spy(tm)
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=456)
    facts = [c for c in captured if c[0] == "run.state.anchor_changed"]
    assert len(facts) == 1, captured
    assert facts[0][3]["pid"] == 456


# ---------------------------------------------------------------------------
# run_state_remove / _prune_dead_entries — "run.state.removed"
# ---------------------------------------------------------------------------

def test_run_state_remove_publishes_removed_explicit_reason() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=None)
    captured = _spy(tm)
    tm.run_state_remove("sid-1", "run-1")
    facts = [c for c in captured if c[0] == "run.state.removed"]
    assert len(facts) == 1, captured
    assert facts[0][2] == "run-1"
    assert facts[0][3]["reason"] == "explicit"


def test_prune_dead_entries_publishes_removed_pruned_reason_for_dead_pid() -> None:
    tm = _tm()
    # A pid that is certainly not alive (0 is never a real user process on
    # any platform this backend targets); _pid_alive(0) is False.
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=999999999)
    tm.active_run_ids.pop("sid-1", None)  # not owned by a live turn coroutine
    tm.cancel_events.pop("sid-1", None)
    captured = _spy(tm)
    changed = tm._prune_dead_entries("sid-1")
    assert changed is True
    facts = [c for c in captured if c[0] == "run.state.removed"]
    assert len(facts) == 1, captured
    assert facts[0][2] == "run-1"
    assert facts[0][3]["reason"] == "pruned_dead_pid"


# ---------------------------------------------------------------------------
# run_state_set_pid / run_state_clear_pid — "run.state.pid_changed"
# ---------------------------------------------------------------------------

def test_run_state_set_pid_publishes_pid_changed() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=None)
    captured = _spy(tm)
    tm.run_state_set_pid("sid-1", "run-1", 42)
    facts = [c for c in captured if c[0] == "run.state.pid_changed"]
    assert len(facts) == 1, captured
    assert facts[0][3]["pid"] == 42


def test_run_state_clear_pid_publishes_pid_changed_none() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=42)
    captured = _spy(tm)
    tm.run_state_clear_pid("sid-1", "run-1")
    facts = [c for c in captured if c[0] == "run.state.pid_changed"]
    assert len(facts) == 1, captured
    assert facts[0][3]["pid"] is None


# ---------------------------------------------------------------------------
# run_state_mark_retrying — "run.state.retrying"
# ---------------------------------------------------------------------------

def test_run_state_mark_retrying_publishes_retrying() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", pid=42)
    captured = _spy(tm)
    tm.run_state_mark_retrying("sid-1", "run-1")
    facts = [c for c in captured if c[0] == "run.state.retrying"]
    assert len(facts) == 1, captured
    assert facts[0][1:3] == ("sid-1", "run-1")


# ---------------------------------------------------------------------------
# _run_state_set_target — "run.state.anchor_changed", change-only
# ---------------------------------------------------------------------------

def test_run_state_set_target_publishes_anchor_changed_on_real_change() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", target_message_id="msg-a")
    captured = _spy(tm)
    tm._run_state_set_target("sid-1", "run-1", "msg-b")
    facts = [c for c in captured if c[0] == "run.state.anchor_changed"]
    assert len(facts) == 1, captured
    assert facts[0][3]["target_message_id"] == "msg-b"


def test_run_state_set_target_is_silent_when_unchanged() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native", target_message_id="msg-a")
    captured = _spy(tm)
    tm._run_state_set_target("sid-1", "run-1", "msg-a")
    assert captured == [], captured


# ---------------------------------------------------------------------------
# run_state_mark_provider_submitted / run_state_record_activity /
# _update_startup_stalls — "run.state.startup_phase_changed"
# ---------------------------------------------------------------------------

def test_mark_provider_submitted_publishes_startup_phase_changed() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native")
    captured = _spy(tm)
    tm.run_state_mark_provider_submitted("sid-1", "run-1", "codex", silence_threshold_seconds=30)
    facts = [c for c in captured if c[0] == "run.state.startup_phase_changed"]
    assert len(facts) == 1, captured
    fields = facts[0][3]
    assert fields["startup_phase"] == "awaiting_provider_start"
    assert fields["provider_kind"] == "codex"
    assert fields["startup_silence_threshold_seconds"] == 30
    assert fields["stalled_at"] is None


def test_record_activity_publishes_startup_phase_changed_on_ack() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native")
    tm.run_state_mark_provider_submitted("sid-1", "run-1", "claude", silence_threshold_seconds=30)
    captured = _spy(tm)
    ok = tm.run_state_record_activity("sid-1", "run-1", "provider_response")
    assert ok is True
    facts = [c for c in captured if c[0] == "run.state.startup_phase_changed"]
    assert len(facts) == 1, captured
    assert facts[0][3]["startup_phase"] == "running"
    assert facts[0][3]["stalled_at"] is None
    # A normal (non-recovery) activity must NOT also fire the recovery fact.
    assert not [c for c in captured if c[0] == "run.state.recovery_classified"]


def test_record_activity_recovered_run_publishes_recovery_classified_and_phase() -> None:
    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native")
    tm.run_state_mark_provider_submitted("sid-1", "run-1", "claude", silence_threshold_seconds=30)
    captured = _spy(tm)
    ok = tm.run_state_record_activity("sid-1", "run-1", "recovered_run")
    assert ok is True
    recovery_facts = [c for c in captured if c[0] == "run.state.recovery_classified"]
    assert len(recovery_facts) == 1, captured
    assert recovery_facts[0][3]["classification"] == "recovered_at_startup"
    phase_facts = [c for c in captured if c[0] == "run.state.startup_phase_changed"]
    assert len(phase_facts) == 1, captured
    assert phase_facts[0][3]["startup_phase"] == "running"


def test_update_startup_stalls_publishes_startup_phase_changed_stalled() -> None:
    import datetime as _dt

    tm = _tm()
    tm.run_state_add("sid-1", run_id="run-1", kind="native")
    tm.run_state_mark_provider_submitted("sid-1", "run-1", "codex", silence_threshold_seconds=1)
    # Backdate the phase-start timestamp well past the 1s threshold —
    # avoids a real sleep.
    entry = tm._run_state["sid-1"][0]
    entry["startup_phase_started_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=10)
    ).isoformat()
    captured = _spy(tm)
    changed = tm._update_startup_stalls("sid-1")
    assert changed is True
    facts = [c for c in captured if c[0] == "run.state.startup_phase_changed"]
    assert len(facts) == 1, captured
    assert facts[0][3]["startup_phase"] == "stalled"
    assert facts[0][3]["stalled_at"] is not None


# ---------------------------------------------------------------------------
# End-to-end: `_publish_run_state_fact` really reaches the bus (not just
# the spy's recorded call) — proves `publish_threadsafe` delivers to a
# real, loop-pinned subscriber.
# ---------------------------------------------------------------------------

def test_publish_run_state_fact_delivers_via_real_bus_subscriber() -> None:
    async def _go() -> None:
        captured: list[BusEvent] = []

        async def _handler(ev: BusEvent) -> None:
            captured.append(ev)

        bus.subscribe(
            "run.state.*", _handler, name="test-run-state-facts-e2e",
            bind_current_loop=True,
        )
        try:
            tm = _tm()
            tm.run_state_add("sid-e2e", run_id="run-e2e", kind="native", pid=7)
            # `publish_threadsafe` hands off via `call_soon_threadsafe`,
            # which needs at least one loop iteration to run the
            # scheduled `_kick` -> `_drain` -> `await sub.handler(...)`
            # chain. Poll (bounded) instead of a fixed sleep — reacts to
            # the actual delivery rather than guessing a duration.
            for _ in range(200):
                if captured:
                    break
                await asyncio.sleep(0.01)
            assert captured, "run.state.anchor_changed never reached the real subscriber"
            assert captured[0].type == "run.state.anchor_changed"
            assert captured[0].run_id == "run-e2e"
            assert captured[0].payload["pid"] == 7
        finally:
            bus.unsubscribe("test-run-state-facts-e2e")

    asyncio.run(_go())


_TESTS = [
    test_run_state_add_new_entry_publishes_anchor_changed,
    test_run_state_add_existing_entry_update_publishes_anchor_changed,
    test_run_state_remove_publishes_removed_explicit_reason,
    test_prune_dead_entries_publishes_removed_pruned_reason_for_dead_pid,
    test_run_state_set_pid_publishes_pid_changed,
    test_run_state_clear_pid_publishes_pid_changed_none,
    test_run_state_mark_retrying_publishes_retrying,
    test_run_state_set_target_publishes_anchor_changed_on_real_change,
    test_run_state_set_target_is_silent_when_unchanged,
    test_mark_provider_submitted_publishes_startup_phase_changed,
    test_record_activity_publishes_startup_phase_changed_on_ack,
    test_record_activity_recovered_run_publishes_recovery_classified_and_phase,
    test_update_startup_stalls_publishes_startup_phase_changed_stalled,
    test_publish_run_state_fact_delivers_via_real_bus_subscriber,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"ok      {fn.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL    {fn.__name__}")
            import traceback
            traceback.print_exc()
        except Exception:
            failures += 1
            print(f"ERROR   {fn.__name__}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
