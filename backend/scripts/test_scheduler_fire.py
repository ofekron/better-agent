"""Unit tests for scheduler.Scheduler firing semantics — fire_due,
fire_task_triggers, broadcast_schedules, and the start/shutdown lifecycle.

Dual-purpose: pytest-collectible (`async def test_*` below) AND runnable
standalone (`python scripts/test_scheduler_fire.py` runs the legacy
fire_due scenario ladder in main()). The pytest tests are hermetic —
real disk-backed schedule_store, stubbed coordinator/session_manager/bus,
and monkeypatched task_runner/task_script/task_trigger_store — and lock
every branch: due → submit_prompt(source="schedule", user_initiated=False)
with session-record model/cwd; once deleted after fire; recurring
advanced; session-gone → dropped; provider-suspended → delayed;
submit failure restores a once schedule (and tolerates restore failure);
broadcast exceptions are swallowed per channel; task triggers cover
no-task-id, turn_end stale/current, script detector ok/fail, and every
launch-failure retry path.
"""
import asyncio
import contextlib
import os
import shutil
import sys
from datetime import datetime, timedelta

import pytest

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sched-fire-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import scheduler as scheduler_mod  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from stores import schedule_store  # noqa: E402
from stores import task_trigger_store  # noqa: E402
from task_script import ScriptResult  # noqa: E402

# Hermetic pytest tests below run under anyio; the standalone main() ladder
# keeps using asyncio.run directly.
pytestmark = pytest.mark.anyio

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _StubSessions:
    def __init__(self, sessions: dict):
        self._sessions = sessions

    def get(self, sid):
        return self._sessions.get(sid)


class _StubCoordinator:
    def __init__(self, *, raise_on_submit: bool = False, submit_exc=None,
                 dispatch_raises: bool = False, global_raises: bool = False):
        self.submitted: list[tuple[str, dict]] = []
        self.dispatched: list[dict] = []
        self.global_broadcasts: list[tuple[str, dict]] = []
        self.raise_on_submit = raise_on_submit
        self.submit_exc = submit_exc
        self.dispatch_raises = dispatch_raises
        self.global_raises = global_raises

    def submit_prompt(self, sid, params):
        if self.raise_on_submit:
            raise self.submit_exc or RuntimeError("queue locked")
        self.submitted.append((sid, params))
        return "item-id"

    async def dispatch_raw(self, sid, event):
        if self.dispatch_raises:
            raise RuntimeError("dispatch boom")
        self.dispatched.append(event)

    async def broadcast_global(self, name, data):
        if self.global_raises:
            raise RuntimeError("global boom")
        self.global_broadcasts.append((name, data))


class _FakeBus:
    """Stand-in for event_bus.bus; publish is a no-op (or raises)."""
    def __init__(self, *, raises: bool = False):
        self.published: list = []
        self.raises = raises

    async def publish(self, event):
        if self.raises:
            raise RuntimeError("bus boom")
        self.published.append(event)


def _make_due(rec):
    """Pin schedule_store.due to return exactly one real record, isolating
    a fire_due test from any other schedules left in the shared test home."""
    def _due(_now=None):
        live = schedule_store.get(rec["id"]) if rec else None
        return [live] if live else []
    return _due


def _seed_provider(monkeypatch, *, suspended=False, default=""):
    import config_store
    monkeypatch.setattr(config_store, "default_session_provider_id", lambda: default)
    monkeypatch.setattr(config_store, "provider_suspended", lambda pid: suspended)


async def _make_sched(monkeypatch, sessions, *, bus_raises=False,
                      dispatch_raises=False, global_raises=False,
                      raise_on_submit=False):
    monkeypatch.setattr(scheduler_mod, "session_manager", _StubSessions(sessions))
    monkeypatch.setattr(scheduler_mod, "bus", _FakeBus(raises=bus_raises))
    return _StubCoordinator(raise_on_submit=raise_on_submit,
                            dispatch_raises=dispatch_raises,
                            global_raises=global_raises)


async def test_fire_due_happy_once_submits_and_deletes(monkeypatch):
    _seed_provider(monkeypatch)
    coord = await _make_sched(monkeypatch, {"s1": {"model": "m1", "cwd": "/c"}})
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="do it", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 1
    sid, params = coord.submitted[0]
    assert sid == "s1" and params["prompt"] == "do it"
    assert params["source"] == "schedule" and params["user_initiated"] is False
    assert params["model"] == "m1" and params["cwd"] == "/c"
    assert schedule_store.get(rec["id"]) is None  # once deleted after fire
    assert coord.dispatched and coord.global_broadcasts  # broadcast happy path


async def test_fire_due_session_gone_drops_schedule(monkeypatch):
    coord = await _make_sched(monkeypatch, {})  # "ghost" not present
    now = datetime.now()
    rec = schedule_store.create(app_session_id="ghost", prompt="x", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 0 and not coord.submitted
    assert schedule_store.get(rec["id"]) is None  # dropped


async def test_fire_due_provider_suspended_delays(monkeypatch):
    _seed_provider(monkeypatch, suspended=True)
    coord = await _make_sched(
        monkeypatch, {"s1": {"model": "m", "cwd": "/c", "provider_id": "p1"}})
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="x", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 0 and not coord.submitted
    assert schedule_store.get(rec["id"]) is not None  # delayed, not fired


async def test_fire_due_provider_check_failure_still_fires(monkeypatch):
    # If the provider-suspension check itself raises, the tick must NOT abort —
    # it logs and falls through to a normal fire.
    import config_store
    # A truthy default provider_id forces provider_suspended() to be evaluated
    # (and raise) inside the guarded check.
    monkeypatch.setattr(config_store, "default_session_provider_id", lambda: "p1")

    def _boom(_pid):
        raise RuntimeError("cfg boom")
    monkeypatch.setattr(config_store, "provider_suspended", _boom)
    coord = await _make_sched(monkeypatch, {"s1": {"model": "m", "cwd": "/c"}})
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="do it", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 1 and coord.submitted  # exception swallowed → fired anyway


async def test_fire_due_submit_fail_once_restores(monkeypatch):
    _seed_provider(monkeypatch)
    coord = await _make_sched(monkeypatch, {"s1": {"model": "m", "cwd": "/c"}},
                              raise_on_submit=True)
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="boom", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 0
    restored = [r for r in schedule_store.list_for_session("s1") if r["prompt"] == "boom"]
    assert restored and datetime.fromisoformat(restored[0]["fire_at"]) > now


async def test_fire_due_submit_fail_once_restore_value_error(monkeypatch):
    _seed_provider(monkeypatch)
    coord = await _make_sched(monkeypatch, {"s1": {"model": "m", "cwd": "/c"}},
                              raise_on_submit=True)
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="nope", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    # Restore path calls schedule_store.create — make it fail fail-closed.

    def _raise(**_kw):
        raise ValueError("nope")
    monkeypatch.setattr(schedule_store, "create", _raise)
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 0
    nope = [r for r in schedule_store.list_for_session("s1") if r["prompt"] == "nope"]
    assert nope == []  # mark_fired deleted it; restore failed → not re-created


async def test_fire_due_submit_fail_recurring_advances_no_restore(monkeypatch):
    _seed_provider(monkeypatch)
    coord = await _make_sched(monkeypatch, {"s1": {"model": "m", "cwd": "/c"}},
                              raise_on_submit=True)
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="tick", kind="recurring",
                                interval_seconds=60,
                                fire_at=(now - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 0
    recs = [r for r in schedule_store.list_for_session("s1") if r["kind"] == "recurring"]
    # mark_fired advanced it past now; submit failed but recurring isn't restored.
    assert len(recs) == 1 and datetime.fromisoformat(recs[0]["fire_at"]) > now


async def test_broadcast_schedules_all_channels_raise_isolated(monkeypatch):
    _seed_provider(monkeypatch)
    coord = await _make_sched(monkeypatch, {"s1": {"model": "m", "cwd": "/c"}},
                              bus_raises=True, dispatch_raises=True,
                              global_raises=True)
    now = datetime.now()
    rec = schedule_store.create(app_session_id="s1", prompt="do it", kind="once",
                                fire_at=(now - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(schedule_store, "due", _make_due(rec))
    # submit succeeds; broadcast_schedules then raises on every channel and
    # each exception is swallowed independently — the tick still returns fired=1.
    fired = await Scheduler(coord).fire_due(now)
    assert fired == 1
    assert not coord.dispatched and not coord.global_broadcasts


async def test_start_is_idempotent_and_shutdown_cancels():
    sched = Scheduler(_StubCoordinator(), tick_interval=10.0)
    sched.start()
    first = sched._task
    assert first is not None and not first.done()
    sched.start()  # already running → no-op
    assert sched._task is first
    await sched.shutdown()
    assert sched._task is None
    assert first.done()


async def test_shutdown_without_task_is_noop():
    sched = Scheduler(_StubCoordinator())
    assert sched._task is None
    await sched.shutdown()
    assert sched._task is None


async def test_noop_ws_callback_returns_none():
    # submit_prompt params carry this as a placeholder ws_callback until the
    # per-session processor swaps in the real dispatcher.
    assert await scheduler_mod._noop_ws_callback({"type": "x"}) is None


async def test_loop_ticks_and_swallows_exceptions(monkeypatch):
    # Drive _loop directly: a couple of clean ticks, one tick whose fire_due
    # raises (exercising the swallow-except), then cancellation (exercising
    # the CancelledError re-raise). Bounded by cancelling the task.
    sched = Scheduler(_StubCoordinator(), tick_interval=0.01)
    ticks = {"n": 0}

    async def _fire_due(now=None):
        ticks["n"] += 1
        if ticks["n"] == 2:
            raise RuntimeError("tick boom")
        return 0

    sched.fire_due = _fire_due  # type: ignore[assignment]
    task = asyncio.create_task(sched._loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert ticks["n"] >= 2  # at least one clean + one raising tick


async def test_loop_cancellation_reraises():
    # Cancellation delivered while a tick is mid-flight (inside fire_due, which
    # is inside _loop's try) must re-raise CancelledError, not be swallowed.
    sched = Scheduler(_StubCoordinator(), tick_interval=10.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_fire(now=None):
        entered.set()
        await release.wait()
        return 0

    sched.fire_due = _blocking_fire  # type: ignore[assignment]
    task = asyncio.create_task(sched._loop())
    await entered.wait()  # fire_due is now awaiting inside the try
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


def _patch_trigger_store(monkeypatch, recs):
    """Pin due() to recs and spy on mark_fired/retry_later; snapshot is
    controlled per-test by reassigning the returned dict's `current` flag."""
    monkeypatch.setattr(task_trigger_store, "due", lambda now=None: list(recs))
    fired: list[str] = []
    monkeypatch.setattr(task_trigger_store, "mark_fired",
                        lambda tid, now=None: fired.append(tid))
    retries: list[str] = []
    monkeypatch.setattr(task_trigger_store, "retry_later",
                        lambda tid, now=None: retries.append(tid))
    snap = {"current": True, "calls": 0}
    monkeypatch.setattr(task_trigger_store, "receipt_task_snapshot",
                        lambda tid: _snapshot(snap))
    return fired, retries, snap


def _snapshot(snap):
    snap["calls"] += 1
    # First call (pre-launch gate) is current; later calls reflect snap["current"].
    if snap["calls"] == 1:
        return True, None
    return snap["current"], None


def _patch_task_runner(monkeypatch, launch):
    import task_runner
    async def _broadcast(*args, **kwargs):
        return None
    monkeypatch.setattr(task_runner, "broadcast_tasks_changed", _broadcast)
    monkeypatch.setattr(task_runner, "launch_task", launch)
    return task_runner


async def test_trigger_without_task_id_marks_and_skips(monkeypatch):
    fired, _, _ = _patch_trigger_store(monkeypatch, [{"id": "t1"}])
    async def _launch(*a, **k):
        raise AssertionError("must not launch without task_id")
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0 and fired == ["t1"]


async def test_trigger_script_detector_fail_skips_launch(monkeypatch):
    import task_script
    fired, _, _ = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "script",
                       "detector": "exit 1"}])
    monkeypatch.setattr(task_script, "run_script",
                        lambda detector, timeout=30: ScriptResult(1, "", "", False))
    launches: list = []
    async def _launch(*a, **k):
        launches.append(a)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0 and launches == []
    assert fired == ["t1"]  # poll window advanced even on detector miss


async def test_trigger_script_detector_ok_launches(monkeypatch):
    import task_script
    fired, _, _ = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "script",
                       "detector": "true"}])
    monkeypatch.setattr(task_script, "run_script",
                        lambda detector, timeout=30: ScriptResult(0, "", "", False))
    launches: list = []
    async def _launch(*a, **k):
        launches.append(a)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 1 and len(launches) == 1
    # script kind: marked on the poll-window advance path, not post-launch.
    assert fired == ["t1"]


async def test_trigger_schedule_kind_marks_then_launches(monkeypatch):
    fired, _, _ = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "schedule"}])
    launches: list = []
    async def _launch(*a, **k):
        launches.append(a)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 1 and len(launches) == 1
    assert fired == ["t1"]  # non-turn_end/non-script marked before launch


async def test_trigger_turn_end_current_launches_and_marks(monkeypatch):
    fired, _, snap = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "turn_end_once"}])
    launches: list = []
    async def _launch(*a, **k):
        launches.append(a)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 1 and len(launches) == 1
    assert fired == ["t1"]  # turn_end marked post-launch on success


async def test_trigger_turn_end_stale_at_gate_skips(monkeypatch):
    # Pre-launch snapshot says the receipt is no longer current → mark_fired
    # and continue WITHOUT launching.
    monkeypatch.setattr(task_trigger_store, "due", lambda now=None: [
        {"id": "t1", "task_id": "task1", "kind": "turn_end_once"}])
    fired: list[str] = []
    monkeypatch.setattr(task_trigger_store, "mark_fired",
                        lambda tid, now=None: fired.append(tid))
    monkeypatch.setattr(task_trigger_store, "receipt_task_snapshot",
                        lambda tid: (False, None))
    async def _launch(*a, **k):
        raise AssertionError("stale turn_end must not launch")
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0 and fired == ["t1"]


async def test_trigger_turn_end_nonretryable_failure_marks_and_continues(monkeypatch):
    import task_runner
    fired, _, _ = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "turn_end_once"}])
    async def _launch(*a, **k):
        raise task_runner.TaskLaunchError("boom", retryable=False)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0 and fired == ["t1"]  # non-retryable → mark_fired, no retry


async def test_trigger_turn_end_retryable_current_retries_later(monkeypatch):
    import task_runner
    fired, retries, snap = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "turn_end_once"}])
    snap["current"] = True  # both snapshot calls current
    async def _launch(*a, **k):
        raise task_runner.TaskLaunchError("boom", retryable=True)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0
    assert retries == ["t1"] and fired == []  # still current → retry_later, not mark_fired


async def test_trigger_turn_end_retryable_not_current_marks(monkeypatch):
    import task_runner
    fired, retries, snap = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "turn_end_once"}])
    snap["current"] = False  # 2nd snapshot (post-failure) stale
    async def _launch(*a, **k):
        raise task_runner.TaskLaunchError("boom", retryable=True)
    _patch_task_runner(monkeypatch, _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0
    assert fired == ["t1"] and retries == []  # stale → mark_fired, no retry


async def test_trigger_non_turn_end_failure_broadcasts(monkeypatch):
    import task_runner
    fired, _, _ = _patch_trigger_store(
        monkeypatch, [{"id": "t1", "task_id": "task1", "kind": "schedule"}])
    broadcasts: list = []

    async def _launch(*a, **k):
        raise RuntimeError("boom")

    async def _broadcast(*args, **kwargs):
        broadcasts.append(args)
    monkeypatch.setattr(task_runner, "broadcast_tasks_changed", _broadcast)
    monkeypatch.setattr(task_runner, "launch_task", _launch)
    launched = await Scheduler(_StubCoordinator()).fire_task_triggers()
    assert launched == 0
    assert fired == ["t1"] and broadcasts  # marked pre-launch; failure still broadcasts


def main() -> int:
    now = datetime.now()
    past = (now - timedelta(minutes=30)).isoformat()

    scheduler_mod.session_manager = _StubSessions({
        "s1": {"model": "m1", "cwd": "/tmp/cwd1"},
    })

    print("T1 due once-schedule fires through submit_prompt and is deleted")
    r_once = schedule_store.create(
        app_session_id="s1", prompt="do it", kind="once", fire_at=past,
    )
    coord = _StubCoordinator()
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 1, "fired exactly one")
    sid, params = coord.submitted[0]
    check(sid == "s1" and params["prompt"] == "do it", "prompt routed to session")
    check(params["source"] == "schedule" and params["user_initiated"] is False,
          "source='schedule', user_initiated=False")
    check(params["model"] == "m1" and params["cwd"] == "/tmp/cwd1",
          "model/cwd from the session record (authoritative)")
    check(schedule_store.get(r_once["id"]) is None, "once deleted after fire")
    check(any(e["type"] == "schedules_updated" for e in coord.dispatched),
          "schedules_updated broadcast after fire")

    print("T2 overdue recurring fires ONCE and advances past now (catch-up)")
    r_rec = schedule_store.create(
        app_session_id="s1", prompt="tick", kind="recurring",
        interval_seconds=60,
        fire_at=(now - timedelta(hours=3)).isoformat(),
    )
    coord = _StubCoordinator()
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 1, "one catch-up fire despite 3h of missed intervals")
    rec = schedule_store.get(r_rec["id"])
    check(rec is not None and datetime.fromisoformat(rec["fire_at"]) > now,
          "recurring advanced past now")
    coord2 = _StubCoordinator()
    check(asyncio.run(Scheduler(coord2).fire_due(now)) == 0,
          "immediately re-ticking fires nothing")
    schedule_store.delete(r_rec["id"])

    print("T3 session gone → schedule dropped, nothing submitted")
    r_ghost = schedule_store.create(
        app_session_id="ghost", prompt="x", kind="once", fire_at=past,
    )
    coord = _StubCoordinator()
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 0 and not coord.submitted, "no submit for missing session")
    check(schedule_store.get(r_ghost["id"]) is None, "ghost schedule dropped")

    print("T4 submit failure is contained (marked fired, no crash)")
    schedule_store.create(
        app_session_id="s1", prompt="boom", kind="once", fire_at=past,
    )
    coord = _StubCoordinator(raise_on_submit=True)
    fired = asyncio.run(Scheduler(coord).fire_due(now))
    check(fired == 0, "failed submit not counted as fired")
    check(schedule_store.due(now) == [],
          "marked before submit → no refire loop on persistent failure")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: scheduler fire path")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
