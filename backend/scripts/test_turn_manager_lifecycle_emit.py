"""Lock test: TurnManager is the sole, exactly-once emitter of
`lifecycle.turn_*` BusEvents across every terminal path.

The (ii) behavior change: every terminal — success, cancel, error,
recovery-finalize — fires exactly one `lifecycle.turn_complete` or
`lifecycle.turn_stopped` on the bus. Worker-inner is deliberately
NOT wired in this isolated commit; it lands at cutover by calling
`_publish_terminal_lifecycle` directly from `_delegation.py`.

Coverage strategy:
- A runtime spy wraps `bus.publish` and inspects the caller frame.
  Catches any second emitter regardless of how the call is spelled
  (direct `bus.publish`, `from event_bus import publish` alias,
  helper indirection).
- A textual "exactly one bus.publish in module" check supplements
  it by failing fast at edit time.
- A static-source guard counts `_publish_terminal_lifecycle(`
  invocations to lock the 5 expected terminal call sites — if a
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
      turn_manager.py: 2 (start + terminal funnels)
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
    assert n_tm == 2, (
        f"expected exactly 2 `bus.publish` calls in turn_manager.py "
        f"(inside lifecycle funnels), got {n_tm} — "
        f"an unexpected emit site exists"
    )
    # Defeat alias-based evasion of the runtime spy.
    assert "= bus.publish" not in tm_src
    assert "from event_bus import publish" not in tm_src
    # Guard against re-introducing the dead public hook.
    assert "publish_worker_inner_terminal" not in tm_src

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
    bypassable: an edit that adds a 6th call inside `run_turn` while
    removing the recovery emit would pass a count check but break
    the (ii) contract on the recovery path.

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
        f"(expected run_turn=3). "
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
            "complete", app_session_id="sid-spy-1", reason="success",
        ))
        asyncio.run(tm._publish_terminal_lifecycle(
            "stopped", app_session_id="sid-spy-2", reason="error",
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
    finally:
        _unsubscribe(h)


def test_publish_turn_start_emits_lifecycle_start() -> None:
    captured, h = _subscribe_capture("tm-test-start")
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_turn_start_lifecycle(
            app_session_id="sid-start",
            manager_session_id="agent-1",
        ))
        events = [e for e in captured if e.sid == "sid-start"]
        assert len(events) == 1, f"expected 1 emit, got {len(events)}"
        assert events[0].type == "lifecycle.turn_start"
        assert events[0].payload.get("manager_session_id") == "agent-1"
        assert events[0].persist is False
    finally:
        _unsubscribe(h)


def test_publish_terminal_emits_exactly_once_each_stopped_cause() -> None:
    captured, h = _subscribe_capture("tm-test-stopped")
    try:
        tm = TurnManager(_StubCoordinator())
        causes = [
            ("sid-cancel", "cancelled"),
            ("sid-error", "error"),
            ("sid-recovery", "recovery_finalize"),
            ("sid-recovery-failed", "recovery_failed"),
        ]
        for sid, reason in causes:
            asyncio.run(tm._publish_terminal_lifecycle(
                "stopped", app_session_id=sid, reason=reason,
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


def test_publish_terminal_swallows_subscriber_exception() -> None:
    """A misbehaving subscriber must NEVER tear down the turn-finalize
    sequence. The bus emit is wrapped in try/except."""
    async def _bad_handler(ev: BusEvent) -> None:
        raise RuntimeError("subscriber blew up")

    bus.subscribe("lifecycle.turn_*", _bad_handler, name="tm-bad-sub")
    try:
        tm = TurnManager(_StubCoordinator())
        asyncio.run(tm._publish_terminal_lifecycle(
            "complete", app_session_id="sid-bad-sub", reason="success",
        ))
    finally:
        _unsubscribe(_bad_handler)


if __name__ == "__main__":
    test_textual_check_no_other_bus_publish_in_module()
    test_run_turn_source_calls_helper_at_every_terminal()
    test_run_turn_records_provider_result_before_user_done()
    test_runtime_spy_bus_publish_only_through_helper()
    test_publish_terminal_emits_exactly_once_complete()
    test_publish_terminal_emits_exactly_once_each_stopped_cause()
    test_user_lifecycle_routes_through_user_prompt_manager()
    test_publish_terminal_swallows_subscriber_exception()
    print("OK: TurnManager sole lifecycle emitter — runtime + structural")
