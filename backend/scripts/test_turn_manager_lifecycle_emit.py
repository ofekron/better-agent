"""Lock test: TurnManager is the sole, exactly-once emitter of live
`lifecycle.turn_*` BusEvents.

Every live terminal — success, cancel, error — fires exactly one
`lifecycle.turn_complete` or `lifecycle.turn_stopped` on the bus.
Recovery projection terminalization belongs to LifecycleStateTree reconciliation;
worker execution uses distinct worker-scoped facts.

Coverage strategy:
- A runtime spy wraps `bus.publish` and inspects the caller frame.
  Catches any second emitter regardless of how the call is spelled
  (direct `bus.publish`, `from event_bus import publish` alias,
  helper indirection).
- A textual "exactly one bus.publish in module" check supplements
  it by failing fast at edit time.
- A static-source guard counts `_publish_terminal_lifecycle(`
  invocations to lock the 4 expected live terminal call sites — if a
  future edit silently drops one, this test fails.
- Subscribers used for capture are removed in `finally` blocks so
  the bus subscriber list doesn't accumulate across tests in the
  same process.
"""
import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home
_test_home.isolate("bc_test_tm_emit_")

from event_bus import BusEvent, bus  # noqa: E402
from turn_manager import TurnManager  # noqa: E402


_IDENTITY = {
    "user_turn_id": "user-turn",
    "execution_turn_id": "execution-turn",
    "lifecycle_message_id": "lifecycle-message",
    "assistant_message_id": "assistant-message",
}


class _StubCoordinator:
    """Minimal Coordinator stub. `_publish_terminal_lifecycle` reaches
    only `session_manager._root_id_for` (module global), not
    Coordinator, so an empty stub suffices for emit-helper tests."""


def _subscribe_capture(name: str) -> tuple[list[BusEvent], object]:
    captured: list[BusEvent] = []

    async def _handler(ev: BusEvent) -> None:
        captured.append(ev)

    bus.subscribe("lifecycle.turn_*", _handler, name=name)
    return captured, _handler


def _unsubscribe(handler) -> None:
    try:
        bus.unsubscribe(handler)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Structural locks — fail at edit time, no runtime needed.
# ---------------------------------------------------------------------------
def test_textual_check_no_other_bus_publish_in_module() -> None:
    """Per-file bus.publish funnels after the UPM split:
      turn_manager.py: 3 (start + user terminal + worker terminal funnels)
      user_prompt_manager.py: 1 (user lifecycle funnel)
    Each file's count locks down exactly-one-funnel-per-responsibility.
    """
    backend = Path(__file__).resolve().parent.parent
    tm_src = (backend / "turn_manager.py").read_text()
    upm_src = (backend / "user_prompt_manager.py").read_text()

    # TurnManager.
    assert "_publish_terminal_lifecycle" in tm_src
    # Count actual call sites (`bus.publish(`), not docstring mentions.
    n_tm = tm_src.count("bus.publish(")
    assert n_tm == 3, (
        f"expected exactly 3 `bus.publish` calls in turn_manager.py "
        f"(inside lifecycle funnels), got {n_tm} — "
        f"an unexpected emit site exists"
    )
    # Defeat alias-based evasion of the runtime spy.
    assert "= bus.publish" not in tm_src
    assert "from event_bus import publish" not in tm_src
    assert "_publish_worker_terminal_lifecycle" in tm_src
    delegation_src = (
        backend / "orchs" / "manager" / "_delegation.py"
    ).read_text()
    assert "_publish_worker_terminal_lifecycle(" in delegation_src
    assert "_publish_terminal_lifecycle(" not in delegation_src

    # UserPromptManager.
    assert "_publish_user_lifecycle" in upm_src
    n_upm = upm_src.count("bus.publish(")
    assert n_upm == 1, (
        f"expected exactly 1 `bus.publish` call in user_prompt_manager.py "
        f"(inside _publish_user_lifecycle), got {n_upm} — "
        f"a second emit site exists"
    )
    assert "= bus.publish" not in upm_src
    assert "from event_bus import publish" not in upm_src

    assert "_publish_turn_start_lifecycle" in tm_src

    # Cross-file: TurnManager must NOT contain a user lifecycle funnel.
    assert "_publish_user_lifecycle" not in tm_src, (
        "user lifecycle funnel must live on UserPromptManager, not TurnManager"
    )
    # And UPM must NOT contain a terminal lifecycle funnel.
    assert "_publish_terminal_lifecycle" not in upm_src, (
        "terminal lifecycle funnel must live on TurnManager, not UserPromptManager"
    )


def test_run_turn_source_calls_helper_at_every_terminal() -> None:
    """Lock the per-function distribution of `_publish_terminal_lifecycle(`
    call sites — not just the global count. Cardinality alone is
    bypassable: moving or replacing one live terminal call could preserve
    a global count while breaking the per-path contract.

    Expected:
      run_turn: success-complete, failed-result-stopped,
                cancel-stopped, error-stopped                        = 4
    """
    import ast
    src = (Path(__file__).resolve().parent.parent / "turn_manager.py").read_text()
    tree = ast.parse(src)
    per_func: dict[str, int] = {}
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if cls.name != "TurnManager":
            continue
        for fn in (n for n in cls.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))):
            count = 0
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "_publish_terminal_lifecycle"
                    and isinstance(f.value, ast.Name) and f.value.id == "self"
                ):
                    count += 1
            if count:
                per_func[fn.name] = count
    assert per_func == {
        "run_turn": 4,
    }, (
        f"per-function `_publish_terminal_lifecycle(` distribution "
        f"diverged from the (ii) contract: {per_func} "
        f"(expected run_turn=4). "
        f"A terminal emit was added, dropped, or relocated to the "
        f"wrong method."
    )


def test_run_turn_records_provider_result_before_user_done() -> None:
    """Provider failures are data results, not Python exceptions. The
    user_message_done payload must be based on that result; otherwise a
    failed turn reports success when Coordinator.handle_prompt's else
    branch emits the done frame."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "turn_manager.py").read_text()
    tree = ast.parse(src)
    run_turn = None
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if cls.name != "TurnManager":
            continue
        run_turn = next(
            (
                n for n in cls.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_turn"
            ),
            None,
        )
        break
    assert run_turn is not None, "TurnManager.run_turn not found"
    calls = [
        n for n in ast.walk(run_turn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "record_turn_result"
    ]
    assert len(calls) == 1, (
        "run_turn must record the provider result exactly once before "
        "Coordinator emits user_message_done"
    )
    call = calls[0]
    success_kw = next((kw for kw in call.keywords if kw.arg == "success"), None)
    assert success_kw is not None, "record_turn_result must pass success="
    assert (
        isinstance(success_kw.value, ast.Call)
        and isinstance(success_kw.value.func, ast.Name)
        and success_kw.value.func.id == "bool"
        and len(success_kw.value.args) == 1
        and isinstance(success_kw.value.args[0], ast.Call)
        and isinstance(success_kw.value.args[0].func, ast.Attribute)
        and success_kw.value.args[0].func.attr == "get"
        and isinstance(success_kw.value.args[0].func.value, ast.Name)
        and success_kw.value.args[0].func.value.id == "primary_result"
    ), "record_turn_result success must come from bool(primary_result.get('success'))"


# ---------------------------------------------------------------------------
# Runtime locks.
# ---------------------------------------------------------------------------
def test_runtime_spy_bus_publish_only_through_helper() -> None:
    """Monkey-patch `bus.publish`; assert it is only invoked from
    inside `_publish_terminal_lifecycle`'s frame. Catches second
    emitters added via any indirection (alias import, helper
    function, decorator)."""
    real_publish = bus.publish
    callers: list[str] = []

    async def _spy(event: BusEvent) -> None:
        frame = inspect.currentframe()
        caller = frame.f_back.f_code.co_name if frame and frame.f_back else "?"
        callers.append(caller)
        await real_publish(event)

    bus.publish = _spy  # type: ignore[assignment]
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_terminal_lifecycle(
            "complete",
            app_session_id="sid-spy-1",
            reason="success",
            **_IDENTITY,
        ))
        asyncio.run(tm._publish_terminal_lifecycle(
            "stopped",
            app_session_id="sid-spy-2",
            reason="error",
            **_IDENTITY,
        ))
    finally:
        bus.publish = real_publish  # type: ignore[assignment]

    assert len(callers) == 2, f"expected 2 bus.publish calls, got {len(callers)}"
    for c in callers:
        assert c == "_publish_terminal_lifecycle", (
            f"bus.publish called from {c!r} — must only fire from "
            f"_publish_terminal_lifecycle"
        )


def test_publish_terminal_emits_exactly_once_complete() -> None:
    captured, h = _subscribe_capture("tm-test-complete")
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_terminal_lifecycle(
            "complete",
            app_session_id="sid-success",
            user_turn_id="user-turn",
            execution_turn_id="execution-turn",
            lifecycle_message_id="lifecycle-message",
            assistant_message_id="assistant-message",
            trace_id="trace-1",
            reason="success",
            provider_kind="codex",
        ))
        events = [e for e in captured if e.sid == "sid-success"]
        assert len(events) == 1, f"expected 1 emit, got {len(events)}"
        assert events[0].type == "lifecycle.turn_complete"
        assert events[0].payload.get("reason") == "success"
        assert events[0].payload.get("trace_id") == "trace-1"
        assert events[0].payload.get("provider_kind") == "codex"
        assert events[0].payload.get("user_turn_id") == "user-turn"
        assert events[0].payload.get("execution_turn_id") == "execution-turn"
        assert (
            events[0].payload.get("lifecycle_message_id")
            == "lifecycle-message"
        )
        assert (
            events[0].payload.get("assistant_message_id")
            == "assistant-message"
        )
    finally:
        _unsubscribe(h)


def test_turn_lifecycle_helpers_require_complete_identity() -> None:
    tm = TurnManager(_StubCoordinator())
    for invoke in (
        lambda: tm._publish_turn_start_lifecycle(
            app_session_id="missing",
            **{**_IDENTITY, "assistant_message_id": ""},
        ),
        lambda: tm._publish_terminal_lifecycle(
            "complete",
            app_session_id="missing",
            **{**_IDENTITY, "execution_turn_id": ""},
        ),
    ):
        try:
            asyncio.run(invoke())
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete lifecycle identity was accepted")


def test_publish_turn_start_emits_lifecycle_start() -> None:
    captured, h = _subscribe_capture("tm-test-start")
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_turn_start_lifecycle(
            app_session_id="sid-start",
            user_turn_id="user-turn",
            execution_turn_id="execution-turn",
            lifecycle_message_id="lifecycle-message",
            assistant_message_id="assistant-message",
            manager_session_id="agent-1",
        ))
        events = [e for e in captured if e.sid == "sid-start"]
        assert len(events) == 1, f"expected 1 emit, got {len(events)}"
        assert events[0].type == "lifecycle.turn_start"
        assert events[0].payload.get("manager_session_id") == "agent-1"
        assert events[0].payload.get("user_turn_id") == "user-turn"
        assert events[0].payload.get("execution_turn_id") == "execution-turn"
        assert (
            events[0].payload.get("lifecycle_message_id")
            == "lifecycle-message"
        )
        assert (
            events[0].payload.get("assistant_message_id")
            == "assistant-message"
        )
        assert events[0].persist is False
    finally:
        _unsubscribe(h)


def test_worker_terminal_is_distinct_from_parent_turn_terminal() -> None:
    captured: list[BusEvent] = []

    async def capture(event: BusEvent) -> None:
        captured.append(event)

    bus.subscribe("lifecycle.worker_turn_*", capture, name="worker-terminal")
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_worker_terminal_lifecycle(
            "complete",
            app_session_id="sid-worker",
            delegation_id="delegation",
            execution_turn_id="worker-execution",
            assistant_message_id="parent-assistant",
            provider_run_id="provider-run",
            provider_kind="claude",
        ))
        assert len(captured) == 1
        event = captured[0]
        assert event.type == "lifecycle.worker_turn_complete"
        assert event.run_id == "provider-run"
        assert event.payload["execution_turn_id"] == "worker-execution"
    finally:
        _unsubscribe(capture)


def test_publish_terminal_emits_exactly_once_each_stopped_cause() -> None:
    captured, h = _subscribe_capture("tm-test-stopped")
    try:
        tm = TurnManager(_StubCoordinator())
        causes = [("sid-cancel", "cancelled"), ("sid-error", "error")]
        for sid, reason in causes:
            asyncio.run(tm._publish_terminal_lifecycle(
                "stopped",
                app_session_id=sid,
                reason=reason,
                **_IDENTITY,
            ))
        by_sid: dict[str, list[BusEvent]] = {}
        for e in captured:
            if e.sid in {sid for sid, _ in causes}:
                by_sid.setdefault(e.sid, []).append(e)
        assert set(by_sid) == {sid for sid, _ in causes}
        for sid, events in by_sid.items():
            assert len(events) == 1, f"{sid}: expected 1 emit, got {len(events)}"
            assert events[0].type == "lifecycle.turn_stopped"
    finally:
        _unsubscribe(h)


def test_user_lifecycle_routes_through_user_prompt_manager() -> None:
    """Runtime lock: `user_msg_lifecycle.emit_queued/_sent/_received/_done/
    _failed` must route their bus.publish through
    `UserPromptManager._publish_user_lifecycle`, not via the direct
    fallback path. Spy on `bus.publish` and assert every emit's
    immediate caller frame is `_publish_user_lifecycle`."""
    from orchestrator import _active_coordinator_var
    from user_prompt_manager import UserPromptManager
    import user_msg_lifecycle

    # Install a minimal stub coordinator that exposes both managers so
    # `get_active_coordinator().user_prompt_manager` resolves. The
    # funnel routes through UPM.
    class _MiniCoord:
        def __init__(self):
            self.turn_manager = TurnManager(self)
            self.user_prompt_manager = UserPromptManager(self)
    coord = _MiniCoord()
    token = _active_coordinator_var.set(coord)

    # Stub root resolution so the funnel doesn't bail at root_id lookup.
    from session_manager import manager as sm
    original_root_id_for = sm._root_id_for
    sm._root_id_for = lambda sid: f"root::{sid}"  # type: ignore[assignment]

    real_publish = bus.publish
    callers: list[str] = []
    captured: list[BusEvent] = []

    async def _spy(event: BusEvent) -> None:
        frame = inspect.currentframe()
        caller = frame.f_back.f_code.co_name if frame and frame.f_back else "?"
        callers.append(caller)
        captured.append(event)
        await real_publish(event)

    bus.publish = _spy  # type: ignore[assignment]
    try:
        # Exercise each of the 5 emit_* helpers.
        sid = "sid-user-lc"
        lid = "lc-1"
        asyncio.run(user_msg_lifecycle.emit_queued(
            app_session_id=sid, lifecycle_msg_id=lid, content="hi",
            kind="send", queue_position=0, client_id="client-1",
        ))
        asyncio.run(user_msg_lifecycle.emit_sent(
            app_session_id=sid, lifecycle_msg_id=lid, run_id="r-1",
        ))
        asyncio.run(user_msg_lifecycle.emit_received(
            app_session_id=sid, lifecycle_msg_id=lid,
            agent_user_uuid="uuid-1",
        ))
        asyncio.run(user_msg_lifecycle.emit_done(
            app_session_id=sid, lifecycle_msg_id=lid, success=True,
        ))
        asyncio.run(user_msg_lifecycle.emit_failed(
            app_session_id=sid, lifecycle_msg_id=lid, reason="x",
        ))
    finally:
        bus.publish = real_publish  # type: ignore[assignment]
        sm._root_id_for = original_root_id_for  # type: ignore[assignment]
        _active_coordinator_var.reset(token)

    assert len(callers) == 5, f"expected 5 publishes, got {len(callers)}"
    assert captured[0].payload.get("client_id") == "client-1", (
        "queued lifecycle events must carry client_id so frontend replay "
        "can clear accepted optimistic sends"
    )
    for c in callers:
        assert c == "_publish_user_lifecycle", (
            f"user_message lifecycle bypassed UserPromptManager funnel — "
            f"bus.publish called from {c!r}, not _publish_user_lifecycle"
        )


def test_reported_error_makes_the_done_payload_fail() -> None:
    """A turn that failed must not publish a success-shaped `done`.

    run_turn's error handler reports the failure and returns instead of
    raising, so nothing recorded a sub-turn result. With no sub-turns and
    no error handed over, make_done_payload fell through to its
    "completed cleanly" baseline and published success=True next to an
    `error` event describing the failure.
    """
    from orchs import get_strategy

    strategy = get_strategy("native")

    payload = strategy.make_done_payload("life-clean")
    assert payload["success"] is True, "a clean turn must still report success"

    payload = strategy.make_done_payload(
        "life-failed", terminal_error="NodeOffline: node 'lenovo' is offline",
    )
    assert payload["success"] is False, "a reported terminal error must fail the turn"
    assert "NodeOffline" in (payload["error"] or ""), payload["error"]


def test_run_turn_hands_its_reported_error_to_the_done_emit() -> None:
    """Lock the hand-off wiring, not just the payload rule.

    make_done_payload already accepted `terminal_error`, but no caller
    ever supplied it — the parameter was unreachable, so the payload rule
    alone could not fire. Guard both ends of the hand-off.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    turn_src = (backend_dir / "turn_manager.py").read_text(encoding="utf-8")
    orch_src = (backend_dir / "orchestrator.py").read_text(encoding="utf-8")
    upm_src = (backend_dir / "user_prompt_manager.py").read_text(encoding="utf-8")

    assert "_terminal_error_by_session[app_session_id] = error_text" in turn_src, (
        "run_turn's error path no longer hands its failure to the terminal emit"
    )
    assert "_terminal_error_by_session.pop(" in orch_src, (
        "the done-emit caller no longer consumes the handed-over failure"
    )
    assert "terminal_error=terminal_error" in orch_src, (
        "the handed-over failure is not passed to emit_user_msg_done"
    )
    assert "terminal_error=terminal_error" in upm_src, (
        "emit_user_msg_done no longer forwards terminal_error to make_done_payload"
    )


def test_publish_terminal_swallows_subscriber_exception() -> None:
    """A misbehaving subscriber must NEVER tear down the turn-finalize
    sequence. The bus emit is wrapped in try/except."""
    async def _bad_handler(ev: BusEvent) -> None:
        raise RuntimeError("subscriber blew up")

    bus.subscribe("lifecycle.turn_*", _bad_handler, name="tm-bad-sub")
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_terminal_lifecycle(
            "complete",
            app_session_id="sid-bad-sub",
            reason="success",
            **_IDENTITY,
        ))
    finally:
        _unsubscribe(_bad_handler)


def test_reconcile_lifecycle_projection_clears_orphaned_running_indicator() -> None:
    """Regression: the "running" indicator must match the native session's
    PID lifecycle, not linger forever.

    If lifecycle's terminal bus event (turn_complete/turn_stopped) is lost
    after the run_state entry backing it is gone — e.g. an exception during
    finalization — `lifecycle.has_active_session` stays True with no PID
    behind it. `reconcile_lifecycle_projection` must clear that stuck state
    when it finds no live run backing it, so the running indicator can
    self-heal on the recurring background tick instead of only at the next
    full backend restart.
    """
    sid = "sid-reconcile-orphan"

    async def _scenario() -> None:
        tm = TurnManager(_StubCoordinator())
        await tm.lifecycle.publish(
            "lifecycle.turn_start",
            root_id=sid,
            session_id=sid,
            payload=_IDENTITY,
        )
        assert tm.lifecycle.has_active_session(sid) is True, (
            "setup: turn_start must mark the session active"
        )
        # No run_state entry exists for `sid` — the lost-terminal-event
        # scenario: the PID-driven side has nothing to say about this
        # session, but lifecycle still thinks it's running.
        assert tm._run_state.get(sid) is None

        await tm.reconcile_lifecycle_projection()

        assert tm.lifecycle.has_active_session(sid) is False, (
            "reconcile_lifecycle_projection did not clear a lifecycle turn "
            "stuck 'running' with no live PID/run backing it — the running "
            "indicator would stay stuck on forever"
        )

    asyncio.run(_scenario())


def test_reconcile_lifecycle_projection_preserves_between_attempts_retry_gap() -> None:
    """A dead PID on a run the turn coroutine still owns (continuation /
    retry between attempts) must NOT be reconciled away as orphaned — that
    is the exact false-negative `_entry_owned_by_live_turn` exists to
    prevent, and periodic reconciliation must not reintroduce it."""
    sid = "sid-reconcile-retry-gap"
    run_id = "run-retry-1"
    dead_pid = 999_999_999  # never a real PID

    async def _scenario() -> None:
        tm = TurnManager(_StubCoordinator())
        await tm.lifecycle.publish(
            "lifecycle.turn_start",
            root_id=sid,
            session_id=sid,
            payload=_IDENTITY,
        )
        tm.run_state_add(sid, run_id=run_id, kind="native", pid=dead_pid)
        tm.active_run_ids[sid] = [run_id]
        tm.cancel_events[sid] = asyncio.Event()

        assert tm._entry_owned_by_live_turn(sid, {"run_id": run_id}) is True, (
            "setup: the retry-gap entry must be recognized as live-turn-owned"
        )

        await tm.reconcile_lifecycle_projection()

        assert tm.lifecycle.has_active_session(sid) is True, (
            "reconcile_lifecycle_projection killed the running indicator "
            "for an in-flight turn during its between-attempts retry gap"
        )

    asyncio.run(_scenario())


def test_dead_non_retry_run_state_converges_via_prune_then_reconcile() -> None:
    """A dead pid on a run entry that is NOT retry-owned (a crashed/orphaned
    run, not a between-attempts gap) must converge to "not running" through
    the same per-tick sequence `_bg_tick_loop` uses: `tick_running_state`
    (pruning) runs before `reconcile_lifecycle_projection` — pinning the
    ordering `_bg_tick_loop` depends on for this class of divergence to
    self-heal within one tick instead of requiring a backend restart."""
    sid = "sid-reconcile-dead-orphan"
    run_id = "run-dead-orphan-1"
    dead_pid = 999_999_998  # never a real PID

    async def _scenario() -> None:
        tm = TurnManager(_StubCoordinator())
        await tm.lifecycle.publish(
            "lifecycle.turn_start",
            root_id=sid,
            session_id=sid,
            payload=_IDENTITY,
        )
        tm.run_state_add(sid, run_id=run_id, kind="native", pid=dead_pid)
        # No `active_run_ids`/`cancel_events` entry — this run is orphaned,
        # not owned by an in-flight turn coroutine.
        assert tm._entry_owned_by_live_turn(sid, {"run_id": run_id}) is False

        # Same order `_bg_tick_loop` runs every 2s: prune dead entries
        # first, then re-ground the lifecycle projection.
        tm.tick_running_state()
        assert tm._run_state.get(sid) is None, (
            "setup: the dead non-retry entry must be pruned before reconcile"
        )

        await tm.reconcile_lifecycle_projection()

        assert tm.lifecycle.has_active_session(sid) is False, (
            "a dead, non-retry-owned run must converge to 'not running' "
            "within one prune+reconcile tick cycle"
        )

    asyncio.run(_scenario())


def test_bg_tick_loop_prunes_before_scheduling_lifecycle_reconcile() -> None:
    """Structural guard for the ordering the tests above depend on: if a
    future edit reorders `_bg_tick_loop` so lifecycle reconcile is
    scheduled before dead entries are pruned for the same tick, a dead
    non-retry run could be seen as "live" by reconcile one tick longer than
    necessary. Lock the source order so that regression fails loudly."""
    src = Path(__file__).resolve().parent.parent.joinpath(
        "turn_manager.py",
    ).read_text(encoding="utf-8")
    start = src.index("def _bg_tick_loop(")
    end = src.index("def _schedule_lifecycle_reconcile(", start)
    body = src[start:end]
    refresh_idx = body.index("self._refresh_cache()")
    schedule_idx = body.index("self._schedule_lifecycle_reconcile()")
    assert refresh_idx < schedule_idx, (
        "_bg_tick_loop must prune dead run_state entries "
        "(_refresh_cache -> tick_running_state) before scheduling "
        "reconcile_lifecycle_projection, or dead non-retry runs will be "
        "misread as live for an extra tick"
    )


if __name__ == "__main__":
    test_textual_check_no_other_bus_publish_in_module()
    test_run_turn_source_calls_helper_at_every_terminal()
    test_run_turn_records_provider_result_before_user_done()
    test_runtime_spy_bus_publish_only_through_helper()
    test_publish_terminal_emits_exactly_once_complete()
    test_turn_lifecycle_helpers_require_complete_identity()
    test_publish_turn_start_emits_lifecycle_start()
    test_worker_terminal_is_distinct_from_parent_turn_terminal()
    test_publish_terminal_emits_exactly_once_each_stopped_cause()
    test_user_lifecycle_routes_through_user_prompt_manager()
    test_reported_error_makes_the_done_payload_fail()
    test_run_turn_hands_its_reported_error_to_the_done_emit()
    test_publish_terminal_swallows_subscriber_exception()
    test_reconcile_lifecycle_projection_clears_orphaned_running_indicator()
    test_reconcile_lifecycle_projection_preserves_between_attempts_retry_gap()
    test_dead_non_retry_run_state_converges_via_prune_then_reconcile()
    test_bg_tick_loop_prunes_before_scheduling_lifecycle_reconcile()
    print("OK: TurnManager sole lifecycle emitter — runtime + structural")
