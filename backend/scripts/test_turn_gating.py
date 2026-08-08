"""Turn-gating regression tests for the dequeue→cancel_events gap and
recovery-overlap findings.

Locks:
  1. cancel_turn with NO live turn and nothing in flight is a pure
     no-op: returns False and does NOT set _session_cancelled (the
     old unconditional set leaked into the next turn and suppressed
     its supervisor verdict).
  2. cancel_turn with NO live turn but a dequeued prompt mid-gap
     signals that exact queue/lifecycle claim and reports True.
  3. cancel_turn with NO live turn but registered active_run_ids
     (recovered runs) fans the cancel out to them and reports True.
  4. _evict_stale_runs never evicts an entry whose pid is alive.
  5. _drive_cli_run refuses to spawn when its cancel_event is already
     set (pre-begin cancellation displaced the turn before spawn).
  6. wait_for_clear_runs blocks while active_run_ids is non-empty and
     releases when it clears.

Run with:
    cd backend && .venv/bin/python scripts/test_turn_gating.py
"""
import asyncio
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import _test_home
_TMP_HOME = _test_home.isolate("bc_test_gating_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_installation  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from turn_helpers import _is_transient_error, _retry_attempt_limit  # noqa: E402
from turn_manager import TurnManager  # noqa: E402
import turn_manager as turn_manager_mod  # noqa: E402
from continuation import PROVIDER_CAPABILITIES_CHANGED_ERROR  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from event_bus import EventBus  # noqa: E402
from event_bus import bus  # noqa: E402
from lifecycle_command_engine import LifecycleCommandEngine  # noqa: E402
from lifecycle_command_model import (  # noqa: E402
    ExecutionTurnIdentity,
    SelectorIdentity,
    UserTurnIdentity,
)
import lifecycle_command_store  # noqa: E402
from native_sid_compatibility import NativeSidCompatibilityChanged  # noqa: E402
import config_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import session_store  # noqa: E402
import user_prefs  # noqa: E402
from orchestrator import Coordinator  # noqa: E402

_T = TypeVar("_T")


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    assert ok, name


def _hook_run_state_begin_retry(tm: TurnManager) -> asyncio.Event:
    """Patch `tm.run_state_begin_retry` to signal the instant the drive
    loop reaches a retry wait.

    `_drive_cli_run`'s rate-limit/transient branches `await
    self.run_state_begin_retry(...)` immediately before `await
    asyncio.wait_for(cancel_event.wait(), timeout=wait_s)` — this is the
    exact call site tests need to synchronize on instead of a blind sleep.
    `_run_state[app_session_id]` itself is not usable as the signal here:
    these tests call `_drive_cli_run` directly (bypassing the `run_turn`
    wrapper that calls `run_state_add`), so `_run_state` never gets an
    entry and `run_state_begin_retry`'s internal mutations are no-ops.
    Hooking the method call itself is the real, harness-independent event.
    """
    entered = asyncio.Event()
    original = tm.run_state_begin_retry

    async def _begin_retry(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        entered.set()

    tm.run_state_begin_retry = _begin_retry
    return entered


def _run_with_lifecycle(
    tm: TurnManager,
    body: Callable[[], Awaitable[_T]],
) -> _T:
    async def _bound() -> _T:
        await tm.lifecycle.bind()
        try:
            return await body()
        finally:
            await tm.lifecycle.close()

    return asyncio.run(_bound())


async def _start_drive_lifecycle(
    tm: TurnManager,
    *,
    session_id: str,
    turn_run_id: str,
    lifecycle_message_id: str,
    assistant_message_id: str | None = None,
) -> None:
    assistant_id = (
        assistant_message_id
        or str(
            (tm.current_assistant_msgs.get(session_id) or {}).get("id")
            or f"{lifecycle_message_id}-assistant"
        )
    )
    current_assistant = tm.current_assistant_msgs.get(session_id)
    if (
        not isinstance(current_assistant, dict)
        or current_assistant.get("id") != assistant_id
    ):
        tm.current_assistant_msgs[session_id] = {"id": assistant_id}
    await tm.lifecycle.publish(
        "lifecycle.turn_start",
        root_id=session_id,
        session_id=session_id,
        payload={
            "user_turn_id": lifecycle_message_id,
            "execution_turn_id": turn_run_id,
            "lifecycle_message_id": lifecycle_message_id,
            "assistant_message_id": assistant_id,
        },
    )


async def _finish_drive_lifecycle(
    tm: TurnManager,
    session_id: str,
) -> None:
    identity = tm.lifecycle.active_turn_identity(session_id)
    if identity is None:
        return
    await tm.lifecycle.publish(
        "lifecycle.turn_complete",
        root_id=session_id,
        session_id=session_id,
        payload=identity,
    )


def _run_drive_with_lifecycle(
    tm: TurnManager,
    body: Callable[[], Awaitable[_T]],
    *,
    session_id: str,
    turn_run_id: str,
    lifecycle_message_id: str,
) -> _T:
    async def _seeded() -> _T:
        await _start_drive_lifecycle(
            tm,
            session_id=session_id,
            turn_run_id=turn_run_id,
            lifecycle_message_id=lifecycle_message_id,
        )
        try:
            return await body()
        finally:
            await _finish_drive_lifecycle(tm, session_id)

    return _run_with_lifecycle(tm, _seeded)


class _StubCoordinator:
    def __init__(self) -> None:
        self._in_flight_prompts: dict[str, int] = {}
        self._prompt_queues: dict = {}
        self._session_cancelled: dict[str, bool] = {}
        self.fanned_out: list[str] = []
        self.hard_cancelled: list[str] = []
        self.internal_token = "test-token"
        self.lifecycle_commands = _LifecycleCommands()

    def _cancel_turn_fanout(self, run_id: str) -> bool:
        self.fanned_out.append(run_id)
        return True

    def _cancel_recovered_run_fanout(self, run_id: str) -> bool:
        self.hard_cancelled.append(run_id)
        return True

    def provider_for_run(self, sid: str, provider_id=None):
        return self.provider_for_session(sid)

    async def broadcast_session(
        self, *args, **kwargs,
    ) -> None:
        pass


class _LifecycleCommands:
    def __init__(self) -> None:
        self.handoffs: dict[str, tuple[str, SelectorIdentity]] = {}
        self.selector_attempt_decisions: list[str] = []
        self.selector_attempts: dict[
            tuple[str, int, str], SelectorIdentity
        ] = {}
        self.next_selector_generation = 1

    async def selector_handoff_source(self, sid: str, *, role: str):
        return self.handoffs.get(f"{sid}:{role}")

    async def admit_prepared_selector_attempt(
        self,
        session_id: str,
        *,
        selector: SelectorIdentity,
        role: str,
        **_kwargs,
    ):
        decision = (
            self.selector_attempt_decisions.pop(0)
            if self.selector_attempt_decisions
            else "admitted"
        )
        generation = self.next_selector_generation
        self.next_selector_generation += 1
        if decision == "admitted":
            self.selector_attempts[(session_id, generation, role)] = selector
        return decision, generation

    async def attach_selector_native_sid(
        self,
        session_id: str,
        *,
        selector_generation: int,
        role: str,
        native_sid: str,
        **_kwargs,
    ) -> bool:
        selector = self.selector_attempts.pop(
            (session_id, selector_generation, role),
            None,
        )
        if selector is None:
            return False
        orchestration_mode = await asyncio.to_thread(
            session_manager.get_field,
            session_id,
            "orchestration_mode",
        )
        if not isinstance(orchestration_mode, str) or not orchestration_mode:
            return False
        await asyncio.to_thread(
            session_manager.set_agent_sid,
            session_id,
            (
                "supervisor"
                if role == "supervisor"
                else orchestration_mode
            ),
            native_sid,
            provider_id=selector.provider_id,
            model=selector.model,
            runner=selector.runner,
        )
        return True

    async def clear_selector_native_sid(self, *args, **kwargs) -> bool:
        return True

    async def request_active_stop(self, *args, **kwargs) -> None:
        return None


class _UPM:
    @staticmethod
    def get_in_flight_lifecycle_msg_id(sid):
        return None


class _RetryProvider:
    def __init__(
        self,
        outcomes: list[dict],
        *,
        id: str = "codex",
        kind: str = "codex",
        emit_discovered: bool = False,
        reset_seconds: float = 0.5,
        runner: str = "native",
        native_sid_compatibility: dict | None = None,
        compatibility_mismatches: int = 0,
        before_discovery: Callable[[], Awaitable[None]] | None = None,
        on_start: Callable[[dict], None] | None = None,
    ) -> None:
        self._runs: dict = {}
        self.outcomes = outcomes
        self.prompts: list[str] = []
        self.session_ids: list[str | None] = []
        self.continuation_chains: list[list[str] | None] = []
        self.start_arguments: list[dict] = []
        self.cancelled: list[str] = []
        self.prepared = 0
        self.discarded = 0
        self.KIND = kind
        self.id = id
        self.record = {
            "id": id,
            "kind": kind,
            "generation": "00000000-0000-4000-8000-000000000001",
            "revision": 1,
            "execution_revision": 1,
            "runner": runner,
        }
        self._emit_discovered = emit_discovered
        self.reset_seconds = reset_seconds
        self.native_sid_compatibility = native_sid_compatibility or {
            "schema": 1,
            "engine": "claude-native",
            "node_id": "primary",
            "thread_store_root": "/tmp/claude/projects",
            "claude_project_namespace": "-tmp",
        }
        self.compatibility_mismatches = compatibility_mismatches
        self.before_discovery = before_discovery
        self.on_start = on_start

    def prepare_run(self, **kw):
        self.prepared += 1
        return prepare_execution(
            self.record,
            runtime_policy={
                "native_sid_compatibility": self.native_sid_compatibility
            },
            **kw,
        )

    def discard_prepared_execution(self, execution) -> bool:
        execution._mark_cancelled()
        self.discarded += 1
        return True

    def start_run(self, *, execution, loop, queue):
        if self.compatibility_mismatches:
            self.compatibility_mismatches -= 1
            error = NativeSidCompatibilityChanged(
                "remote native SID compatibility changed"
            )
            execution._mark_admission_failed(error)
            execution._mark_spawn_failed(error)
            raise error
        execution._try_commit_spawn()
        execution._mark_spawn_completed()
        kw = execution.start_arguments()
        kw.update({"loop": loop, "queue": queue})
        self.start_arguments.append(dict(kw))
        if self.on_start is not None:
            self.on_start(kw)
        self.prompts.append(kw["prompt"])
        self.session_ids.append(kw.get("session_id"))
        self.continuation_chains.append(kw.get("continuation_chain"))
        run_id = kw["run_id"]
        queue = kw["queue"]
        idx = len(self.prompts) - 1
        payload = self.outcomes[idx]
        context_usage = payload.get("context_usage")
        self._runs[run_id] = type(
            "RunState",
            (),
            {"popen": type("Popen", (), {"pid": os.getpid()})()},
        )()

        async def emit_events() -> None:
            if self.before_discovery is not None:
                await self.before_discovery()
            if self._emit_discovered and payload.get("session_id"):
                queue.put_nowait(
                    type("E", (), {
                        "type": "session_discovered",
                        "data": {"session_id": payload["session_id"]},
                    })()
                )
            if isinstance(context_usage, dict):
                queue.put_nowait(
                    type("E", (), {
                        "type": "context_usage",
                        "data": context_usage,
                    })()
                )
            complete_payload = {
                k: v for k, v in payload.items()
                if k != "context_usage"
            }
            queue.put_nowait(
                type("E", (), {"type": "complete", "data": complete_payload})()
            )

        asyncio.run_coroutine_threadsafe(emit_events(), loop)
        return True

    def is_running(self, run_id: str) -> bool:
        return False

    def cancel_turn(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    def parse_rate_limit(self, error, events):
        return datetime.now(timezone.utc) + timedelta(seconds=self.reset_seconds)


def test_noop_cancel_does_not_leak_session_cancelled() -> None:
    print("T1 pure no-op cancel: False + no _session_cancelled leak")
    c = _StubCoordinator()
    tm = TurnManager(c)
    ok = _run_with_lifecycle(tm, lambda: tm.cancel_turn("sid-1"))
    check("returns False", ok is False)
    check("no _session_cancelled leak", "sid-1" not in c._session_cancelled)
    check("no pre-begin owner", not tm.has_pre_begin_prompt("sid-1"))


def test_gap_cancel_signals_exact_pre_begin() -> None:
    print("T2 gap-window cancel signals exact pre-begin claim")
    c = _StubCoordinator()
    tm = TurnManager(c)
    claim = tm.claim_pre_begin_prompt("sid-2", "queue-9", "lm-9")
    assert claim is not None
    ok = _run_with_lifecycle(
        tm,
        lambda: tm.cancel_turn("sid-2", interrupted_by_msg_id="lm-9"),
    )
    check("returns True", ok is True)
    check("exact claim signalled", claim.cancel_event.is_set())
    check("interrupt identity retained", claim.interrupted_by_msg_id == "lm-9")
    check("_session_cancelled set", c._session_cancelled.get("sid-2") is True)


def test_cancel_fans_out_to_recovered_runs() -> None:
    print("T3 cancel with no cancel_event fans out to active_run_ids")
    c = _StubCoordinator()
    tm = TurnManager(c)
    tm.active_run_ids["sid-3"] = ["run-r1", "run-r2"]
    ok = _run_with_lifecycle(tm, lambda: tm.cancel_turn("sid-3"))
    check("returns True", ok is True)
    check("fanout hit both runs", c.fanned_out == ["run-r1", "run-r2"])
    check("_session_cancelled set", c._session_cancelled.get("sid-3") is True)


def test_recovered_cancel_escalates_when_still_alive() -> None:
    print("T3b recovered cancel escalates after grace if still active")
    c = _StubCoordinator()
    tm = TurnManager(c)
    sid = "sid-3b"
    run_id = "run-recovered"
    tm.active_run_ids[sid] = [run_id]
    tm._run_state[sid] = [{
        "run_id": run_id,
        "kind": "native",
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
    }]
    original = turn_manager_mod._RECOVERED_CANCEL_ESCALATE_AFTER_S
    turn_manager_mod._RECOVERED_CANCEL_ESCALATE_AFTER_S = 0.0

    async def _go() -> None:
        ok = await tm.cancel_turn(sid)
        check("returns True", ok is True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    try:
        _run_with_lifecycle(tm, _go)
    finally:
        turn_manager_mod._RECOVERED_CANCEL_ESCALATE_AFTER_S = original
    check("soft fanout happened", c.fanned_out == [run_id])
    check("hard recovered fanout happened", c.hard_cancelled == [run_id])


def test_evict_stale_skips_alive_pid() -> None:
    print("T4 stale eviction skips alive pid, drops dead pid")
    tm = TurnManager(_StubCoordinator())
    sid = "sid-4"
    tm._run_state[sid] = [
        {"run_id": "run-alive", "kind": "native", "pid": os.getpid()},
        {"run_id": "run-dead", "kind": "native", "pid": 2 ** 30},
        {"run_id": "run-other-kind", "kind": "manager", "pid": 2 ** 30},
    ]
    tm._evict_stale_runs(sid, "native")
    remaining = {r["run_id"] for r in tm._run_state.get(sid, [])}
    check("alive entry kept", "run-alive" in remaining)
    check("dead entry evicted", "run-dead" not in remaining)
    check("other-kind entry untouched", "run-other-kind" in remaining)


def test_prune_dead_run_clears_active_run_id() -> None:
    print("T4b prune dead pid clears active_run_ids")
    c = _StubCoordinator()
    tm = TurnManager(c)
    sid = "sid-4b"
    tm.active_run_ids[sid] = ["run-dead"]
    tm._run_state[sid] = [{
        "run_id": "run-dead",
        "kind": "native",
        "pid": 2 ** 30,
        "started_at": datetime.now().isoformat(),
    }]
    changed = tm._prune_dead_entries(sid)
    check("pruned", changed is True)
    check("run_state removed", sid not in tm._run_state)
    check("active_run_ids removed", sid not in tm.active_run_ids)
    check("has_active_runs false", tm.has_active_runs(sid) is False)


def test_prune_stale_pidless_run_clears_active_run_id() -> None:
    print("T4c prune stale pidless run clears active_run_ids")
    c = _StubCoordinator()
    tm = TurnManager(c)
    sid = "sid-4c"
    tm.active_run_ids[sid] = ["run-pidless"]
    tm._run_state[sid] = [{
        "run_id": "run-pidless",
        "kind": "native",
        "started_at": (
            datetime.now()
            - timedelta(seconds=turn_manager_mod._PIDLESS_RUN_STALE_AFTER_S + 1)
        ).isoformat(),
    }]
    changed = tm._prune_dead_entries(sid)
    check("pruned", changed is True)
    check("run_state removed", sid not in tm._run_state)
    check("active_run_ids removed", sid not in tm.active_run_ids)
    check("has_active_runs false", tm.has_active_runs(sid) is False)


def test_prune_stale_retrying_pidless_run_is_retained() -> None:
    print("T4d prune stale retrying pidless run is retained")
    c = _StubCoordinator()
    tm = TurnManager(c)
    sid = "sid-4d"
    tm.active_run_ids[sid] = ["run-retrying"]
    tm._run_state[sid] = [{
        "run_id": "run-retrying",
        "kind": "native",
        "retrying": True,
        "started_at": (
            datetime.now()
            - timedelta(seconds=turn_manager_mod._PIDLESS_RUN_STALE_AFTER_S + 60)
        ).isoformat(),
    }]
    changed = tm._prune_dead_entries(sid)
    check("not pruned", changed is False)
    check("run_state retained", sid in tm._run_state)
    check("active_run_ids retained", tm.active_run_ids.get(sid) == ["run-retrying"])


def test_codex_initialize_timeout_is_not_transient() -> None:
    print("T4e codex initialize timeout is not transient")
    check(
        "initialize timeout non-transient",
        _is_transient_error(
            "TimeoutError: codex app-server request timed out: initialize",
            [],
        ) is False,
    )
    check(
        "other timeout still transient",
        _is_transient_error("TimeoutError: upstream request timed out", []) is True,
    )
    check(
        "initialize timeout gets one pre-accept retry",
        _retry_attempt_limit(
            "TimeoutError: codex app-server request timed out: initialize",
            [],
        ) == 1,
    )
def test_drive_cli_run_pre_spawn_guard() -> None:
    print("T5 _drive_cli_run with pre-set cancel_event never spawns")
    c = _StubCoordinator()
    tm = TurnManager(c)
    spawned: list[str] = []

    class _Provider:
        KIND = "codex"
        _runs: dict = {}

        def start_run(self, **kw):
            spawned.append(kw.get("run_id"))

    c.provider_for_session = lambda sid: _Provider()

    c.user_prompt_manager = _UPM()

    async def _ws(_e):
        pass

    async def _go() -> dict:
        ev = asyncio.Event()
        ev.set()
        return await tm._drive_cli_run(
            prompt="p",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id="sid-5",
            cancel_event=ev,
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-5",
            lifecycle_message_id="lifecycle-5",
        )

    result = _run_with_lifecycle(tm, _go)
    check("no spawn happened", spawned == [])
    check("result is cancelled-failure", result.get("success") is False)


def test_native_non_user_turn_gets_loopback_credentials() -> None:
    print("T5b native non-user turn gets loopback credentials")
    c = _StubCoordinator()
    tm = TurnManager(c)
    sid = session_manager.create(name="team-target", cwd="/tmp", model="sonnet")["id"]
    provider = _RetryProvider([{
        "success": True,
        "session_id": None,
        "token_usage": None,
    }])
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()

    async def _ws(_e):
        pass

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt="reply to sender",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            user_initiated=False,
            turn_run_id="turn-loopback",
            lifecycle_message_id="lifecycle-loopback",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id="turn-loopback",
        lifecycle_message_id="lifecycle-loopback",
    )
    check("result success", result.get("success") is True)
    check("provider spawned", len(provider.start_arguments) == 1)
    backend_url = str(provider.start_arguments[0].get("backend_url") or "")
    check(
        "loopback backend_url forwarded",
        backend_url.startswith("http://localhost:") or backend_url.startswith("http://127.0.0.1:"),
    )
    check(
        "internal_token forwarded",
        provider.start_arguments[0].get("internal_token") == "test-token",
    )


def test_drive_cli_run_flushes_target_before_spawn() -> None:
    print("T5c _drive_cli_run flushes target and records provider submission")
    c = _StubCoordinator()
    tm = TurnManager(c)
    sid = session_manager.create(name="durable-target", cwd="/tmp", model="sonnet")["id"]
    from orchs import get_strategy
    assistant = get_strategy("native").build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, assistant)
    tm.current_assistant_msgs[sid] = assistant
    durable_checks: list[bool] = []
    threshold_threads: list[str] = []
    emitted_snapshots: list[list[dict]] = []
    turn_run_id = "turn-durable-target"
    tm._run_state[sid] = [{"run_id": turn_run_id}]

    def _load_threshold() -> int:
        threshold_threads.append(threading.current_thread().name)
        return 73

    async def _capture_broadcast(_sid, event_type, payload, **_kwargs) -> None:
        if event_type == "run_state":
            emitted_snapshots.append(payload["runs"])

    tm._load_task_start_silence_seconds = _load_threshold
    c.broadcast_session = _capture_broadcast

    def _capture_start(arguments: dict) -> None:
        target_message_id = arguments.get("target_message_id")
        root = session_store.get_root_tree(sid) or {}
        durable_checks.append(
            any(
                message.get("id") == target_message_id
                for message in root.get("messages") or []
            )
        )

    provider = _RetryProvider([{
        "success": True,
        "session_id": None,
        "token_usage": None,
    }], on_start=_capture_start)
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()

    async def _ws(_e):
        pass

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt="p",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id=turn_run_id,
            lifecycle_message_id="lifecycle-flush-target",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id=turn_run_id,
        lifecycle_message_id="lifecycle-flush-target",
    )
    submitted_state = tm._run_state[sid][0]
    check("result success", result.get("success") is True)
    check("target assistant durable before start_run", durable_checks == [True])
    check(
        "startup threshold loaded on turn-dispatch executor",
        len(threshold_threads) == 1
        and threshold_threads[0].startswith("turn-dispatch"),
    )
    check(
        "startup threshold recorded",
        submitted_state.get("startup_silence_threshold_seconds") == 73,
    )
    check(
        "provider submission recorded",
        submitted_state.get("last_activity_kind") == "provider_submitted",
    )
    check(
        "provider submission emitted",
        bool(emitted_snapshots)
        and emitted_snapshots[0][0].get("startup_silence_threshold_seconds") == 73,
    )


def test_rate_limit_wait_keeps_turn_active_and_cancellable() -> None:
    print("T6 rate-limit retry wait stays active and stop-cancellable")
    c = _StubCoordinator()
    provider = _RetryProvider([{
        "success": False,
        "error": "rate_limit",
        "session_id": "agent-1",
        "token_usage": None,
    }])
    c.provider_for_session = lambda sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)
    ws_events: list[dict] = []

    async def _ws(e):
        ws_events.append(e)

    async def _go() -> tuple[bool, bool, bool, bool, dict]:
        ev = asyncio.Event()
        entered_wait = _hook_run_state_begin_retry(tm)
        task = asyncio.create_task(tm._drive_cli_run(
            prompt="p",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id="sid-rl-cancel",
            cancel_event=ev,
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-rl-cancel",
            lifecycle_message_id="lifecycle-rl-cancel",
        ))
        try:
            await asyncio.wait_for(entered_wait.wait(), timeout=4)
            retrying = True
        except asyncio.TimeoutError:
            retrying = False
        no_terminal = not any(e.get("type") == "complete" for e in ws_events)
        active = bool(tm.active_run_ids.get("sid-rl-cancel"))
        pid_cleared = all(
            "pid" not in r for r in tm._run_state.get("sid-rl-cancel", [])
        )
        ev.set()
        result = await asyncio.wait_for(task, timeout=3)
        return retrying, no_terminal, active, pid_cleared, result

    retrying, no_terminal, active, pid_cleared, result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id="sid-rl-cancel",
        turn_run_id="turn-rl-cancel",
        lifecycle_message_id="lifecycle-rl-cancel",
    )
    check("drive loop reached retry wait", retrying)
    check("no complete emitted during retry wait", no_terminal)
    check("active run id retained during retry wait", active)
    check("dead attempt pid cleared during retry wait", pid_cleared)
    check("stop retrying returns cancelled result", result.get("error") == "cancelled")


def test_rate_limit_retry_spawns_once_then_emits_terminal() -> None:
    print("T7 rate-limit retries once after wait, then emits terminal")
    c = _StubCoordinator()
    provider = _RetryProvider([
        {
            "success": False,
            "error": "rate_limit",
            "session_id": "agent-1",
            "token_usage": None,
        },
        {
            "success": True,
            "session_id": "agent-1",
            "token_usage": {"input_tokens": 1},
        },
    ])
    c.provider_for_session = lambda sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)
    ws_events: list[dict] = []

    async def _ws(e):
        ws_events.append(e)

    async def _go() -> dict:
        ev = asyncio.Event()
        return await tm._drive_cli_run(
            prompt="p",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id="sid-rl-success",
            cancel_event=ev,
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-rl-success",
            lifecycle_message_id="lifecycle-rl-success",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id="sid-rl-success",
        turn_run_id="turn-rl-success",
        lifecycle_message_id="lifecycle-rl-success",
    )
    terminals = [e for e in ws_events if e.get("type") == "complete"]
    check("original prompt sent exactly once per attempt", provider.prompts == ["p", "p"])
    check("exactly one terminal complete emitted", len(terminals) == 1)
    check("terminal is the successful retry", terminals[0]["data"].get("success") is True)
    check("result success", result.get("success") is True)


def test_rate_limit_wait_uses_reset_or_one_minute_fallback() -> None:
    print("T7a rate-limit wait uses parsed reset or one-minute fallback")
    fallback = turn_manager_mod._rate_limit_wait_seconds(None)
    long_reset = datetime.now(timezone.utc) + timedelta(hours=2)
    parsed = turn_manager_mod._rate_limit_wait_seconds(long_reset)
    check("fallback is one minute", 59 <= fallback <= 61)
    check("parsed reset is not capped to ten minutes", parsed > 7000)


def test_rate_limit_wait_can_continue_immediately() -> None:
    print("T7b rate-limit wait can continue immediately")
    session = session_manager.create(name="rate-limit-continue", cwd="/tmp", model="sonnet")
    sid = session["id"]
    c = _StubCoordinator()
    provider = _RetryProvider([
        {
            "success": False,
            "error": "rate_limit",
            "session_id": "agent-rate-limited",
            "token_usage": None,
        },
        {
            "success": True,
            "session_id": "agent-continued",
            "token_usage": {"input_tokens": 1},
        },
    ], reset_seconds=30)
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)

    async def _ws(_e):
        pass

    async def _go() -> dict:
        ev = asyncio.Event()
        tm.cancel_events[sid] = ev
        entered_wait = _hook_run_state_begin_retry(tm)
        task = asyncio.create_task(tm._drive_cli_run(
            prompt="p",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=ev,
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-rl-continue",
            lifecycle_message_id="lifecycle-rl-continue",
        ))
        try:
            await asyncio.wait_for(entered_wait.wait(), timeout=4)
            retrying = True
        except asyncio.TimeoutError:
            retrying = False
        landed = tm.request_immediate_continuation(
            sid,
            "p",
            reason="rate_limit_provider_switch",
        )
        result = await asyncio.wait_for(task, timeout=3)
        return {"retrying": retrying, "landed": landed, **result}

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id="turn-rl-continue",
        lifecycle_message_id="lifecycle-rl-continue",
    )
    check("drive loop reached retry wait", result.get("retrying") is True)
    check("immediate continuation landed", result.get("landed") is True)
    check("turn succeeds after continuation", result.get("success") is True)
    check("fresh prompt is continuation-wrapped", "Previous provider session ids: agent-rate-limited" in provider.prompts[-1])


def test_forced_context_overflow_retries_as_fresh_continuation() -> None:
    print("T7c forced context overflow starts fresh continuation")
    user_prefs.set_context_strategy("continuation")
    session = session_manager.create(name="forced-overflow", cwd="/tmp", model="sonnet")
    sid = session["id"]
    provider = _RetryProvider([{
        "success": True,
        "session_id": "fresh-provider",
        "token_usage": {"input_tokens": 1},
    }])
    c = _StubCoordinator()
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)
    tm.force_context_overflow_once(sid)
    ws_events: list[dict] = []

    async def _ws(e):
        ws_events.append(e)

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt="continue here",
            cwd="/tmp",
            model="sonnet",
            session_id="old-provider",
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-forced-overflow",
            lifecycle_message_id="lifecycle-forced-overflow",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id="turn-forced-overflow",
        lifecycle_message_id="lifecycle-forced-overflow",
    )
    fresh = session_manager.get(sid) or {}
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check("chain persisted old provider sid", fresh.get("continuation_chain") == ["old-provider"])
    check("runner received continuation chain", provider.continuation_chains == [["old-provider"]])
    check("prompt wrapped as continuation", "Previous provider session ids: old-provider" in provider.prompts[0])
    check("result success", result.get("success") is True)


def test_capability_change_retries_as_fresh_continuation() -> None:
    print("T7c provider capability drift starts fresh continuation")
    session = session_manager.create(name="capability-drift", cwd="/tmp", model="gpt")
    sid = session["id"]
    prior_user = {
        "id": "capability-prior-user",
        "role": "user",
        "content": "Why does Agent Board not own MCP services?",
        "events": [],
    }
    prior_assistant = {
        "id": "capability-prior-assistant",
        "role": "assistant",
        "content": "Agent Board owns durable board state; services have separate owners.",
        "completed_at": "2026-07-25T12:00:00Z",
        "events": [],
    }
    current_user = {
        "id": "capability-current-user",
        "role": "user",
        "content": "why",
        "cli_prompt": "STALE WRAPPED CLI PROMPT",
        "events": [],
    }
    target_assistant = {
        "id": "capability-target-assistant",
        "role": "assistant",
        "content": "",
        "events": [],
    }
    session_manager.append_user_msg(sid, prior_user, strict=True)
    session_manager.append_assistant_msg(sid, prior_assistant, strict=True)
    session_manager.append_user_msg(sid, current_user, strict=True)
    session_manager.append_assistant_msg(sid, target_assistant, strict=True)
    provider = _RetryProvider([
        {
            "success": False,
            "session_id": "old-provider",
            "error": PROVIDER_CAPABILITIES_CHANGED_ERROR,
            "token_usage": None,
        },
        {
            "success": True,
            "session_id": "fresh-provider",
            "token_usage": {"input_tokens": 1},
        },
    ])
    c = _StubCoordinator()
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)
    tm.current_assistant_msgs[sid] = target_assistant

    async def _ws(_event):
        pass

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt="STALE RUNTIME PROMPT",
            cwd="/tmp",
            model="gpt",
            session_id="old-provider",
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-capability-drift",
            lifecycle_message_id="lifecycle-capability-drift",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id="turn-capability-drift",
        lifecycle_message_id="lifecycle-capability-drift",
    )
    fresh = session_manager.get(sid) or {}
    check("provider retried once", len(provider.prompts) == 2)
    check("retry starts a fresh provider thread", provider.session_ids == ["old-provider", None])
    check("old provider sid persisted in chain", fresh.get("continuation_chain") == ["old-provider"])
    check(
        "retry prompt explains capability change",
        "Available provider capabilities changed" in provider.prompts[1],
    )
    check(
        "retry prompt includes authoritative prior exchange",
        "Agent Board owns durable board state; services have separate owners."
        in provider.prompts[1],
    )
    check("retry prompt ends with persisted current user", provider.prompts[1].endswith("why"))
    check("retry excludes stale runtime prompt", "STALE RUNTIME PROMPT" not in provider.prompts[1])
    check("retry excludes transformed CLI prompt", "STALE WRAPPED CLI PROMPT" not in provider.prompts[1])
    check("capability-change continuation succeeds", result.get("success") is True)


def test_context_continuation_start_runs_off_loop() -> None:
    print("T7c continuation start runs off loop")
    user_prefs.set_context_strategy("continuation")
    session = session_manager.create(name="off-loop-continuation", cwd="/tmp", model="sonnet")
    sid = session["id"]
    provider = _RetryProvider([{
        "success": True,
        "session_id": "fresh-provider-off-loop",
        "token_usage": {"input_tokens": 1},
    }])
    c = _StubCoordinator()
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)
    tm.force_context_overflow_once(sid)
    loop_thread: dict[str, int] = {}
    start_threads: list[int] = []
    session_get_threads: list[int] = []
    provider_threads: list[int] = []
    strategy_threads: list[int] = []
    original_get = session_manager.get
    original_start = turn_manager_mod.start_continuation_for
    original_strategy = user_prefs.get_context_strategy

    def _recording_get(*args, **kwargs):
        session_get_threads.append(threading.get_ident())
        return original_get(*args, **kwargs)

    def _recording_start(*args, **kwargs):
        start_threads.append(threading.get_ident())
        return original_start(*args, **kwargs)

    def _recording_provider_for_session(_sid):
        provider_threads.append(threading.get_ident())
        return provider

    def _recording_strategy():
        strategy_threads.append(threading.get_ident())
        return original_strategy()

    c.provider_for_session = _recording_provider_for_session

    async def _ws(_e):
        pass

    async def _go() -> dict:
        loop_thread["id"] = threading.get_ident()
        return await tm._drive_cli_run(
            prompt="continue off loop",
            cwd="/tmp",
            model="sonnet",
            session_id="old-provider-off-loop",
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-continuation-off-loop",
            lifecycle_message_id="lifecycle-continuation-off-loop",
        )

    try:
        session_manager.get = _recording_get
        turn_manager_mod.start_continuation_for = _recording_start
        user_prefs.get_context_strategy = _recording_strategy
        result = _run_drive_with_lifecycle(
            tm,
            _go,
            session_id=sid,
            turn_run_id="turn-continuation-off-loop",
            lifecycle_message_id="lifecycle-continuation-off-loop",
        )
    finally:
        session_manager.get = original_get
        turn_manager_mod.start_continuation_for = original_start
        user_prefs.get_context_strategy = original_strategy

    check("continuation start observed", len(start_threads) == 1)
    check(
        "session reads off event loop",
        bool(session_get_threads) and all(
            thread_id != loop_thread.get("id") for thread_id in session_get_threads
        ),
    )
    check(
        "continuation start off event loop",
        bool(start_threads) and start_threads[0] != loop_thread.get("id"),
    )
    check(
        "provider resolution off event loop",
        bool(provider_threads) and all(
            thread_id != loop_thread.get("id") for thread_id in provider_threads
        ),
    )
    check(
        "context strategy reads off event loop",
        bool(strategy_threads) and all(
            thread_id != loop_thread.get("id") for thread_id in strategy_threads
        ),
    )
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check("result success", result.get("success") is True)


def test_selector_native_sid_projection_runs_off_loop() -> None:
    print("T7d selector native SID projection runs off loop")
    engine = LifecycleCommandEngine(EventBus())
    loop_thread: dict[str, int] = {}
    read_threads: list[int] = []
    write_threads: list[int] = []
    original_get_field = session_manager.get_field
    original_set_agent_sid = session_manager.set_agent_sid
    original_ack = lifecycle_command_store.acknowledge_native_sid_projection

    def recording_get_field(*_args, **_kwargs):
        read_threads.append(threading.get_ident())
        return "native"

    def recording_set_agent_sid(*_args, **_kwargs):
        write_threads.append(threading.get_ident())
        return {"id": "projection-session"}

    async def _go() -> bool:
        loop_thread["id"] = threading.get_ident()
        return await engine._apply_selector_projection(
            "projection-session",
            {
                "kind": "native_sid",
                "generation": 1,
                "identity": SelectorIdentity(
                    provider_id="claude",
                    model="sonnet",
                    runner="native",
                ).to_dict(),
                "role": "primary",
                "native_sid": "native-session",
            },
        )

    try:
        session_manager.get_field = recording_get_field
        session_manager.set_agent_sid = recording_set_agent_sid
        lifecycle_command_store.acknowledge_native_sid_projection = (
            lambda *_args, **_kwargs: True
        )
        projected = asyncio.run(_go())
    finally:
        session_manager.get_field = original_get_field
        session_manager.set_agent_sid = original_set_agent_sid
        lifecycle_command_store.acknowledge_native_sid_projection = original_ack

    check("selector native SID projected", projected)
    check(
        "selector projection read off event loop",
        bool(read_threads) and read_threads[0] != loop_thread.get("id"),
    )
    check(
        "selector projection write off event loop",
        bool(write_threads) and write_threads[0] != loop_thread.get("id"),
    )


def test_codex_context_fill_preempts_native_compaction() -> None:
    print("T7c codex context fill preempts native compaction")
    user_prefs.set_context_strategy("continuation")
    session = session_manager.create(name="codex-preempt", cwd="/tmp", model="sonnet")
    sid = session["id"]
    session_manager.set_context_window(sid, 1000)
    session_manager.set_context_tokens(sid, 950)
    provider = _RetryProvider([{
        "success": True,
        "session_id": "fresh-provider",
        "token_usage": {"input_tokens": 1},
    }])
    c = _StubCoordinator()
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)

    async def _ws(_e):
        pass

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt="continue before compact",
            cwd="/tmp",
            model="sonnet",
            session_id="old-provider-preempt",
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-codex-preempt",
            lifecycle_message_id="lifecycle-codex-preempt",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id="turn-codex-preempt",
        lifecycle_message_id="lifecycle-codex-preempt",
    )
    fresh = session_manager.get(sid) or {}
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check(
        "chain persisted preempted provider sid",
        fresh.get("continuation_chain") == ["old-provider-preempt"],
    )
    check("runner received continuation chain", provider.continuation_chains == [["old-provider-preempt"]])
    check("result success", result.get("success") is True)


def test_codex_context_usage_persists_then_preempts_next_turn() -> None:
    print("T7c2 codex context usage persists then preempts next turn")
    user_prefs.set_context_strategy("continuation")
    session = session_manager.create(name="codex-usage-preempt", cwd="/tmp", model="sonnet")
    sid = session["id"]
    provider = _RetryProvider([
        {
            "success": True,
            "session_id": "old-provider-usage",
            "token_usage": {"input_tokens": 1},
            "context_usage": {
                "context_window": 1000,
                "context_tokens": 950,
            },
        },
        {
            "success": True,
            "session_id": "fresh-provider-usage",
            "token_usage": {"input_tokens": 1},
        },
    ])
    c = _StubCoordinator()
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)

    async def _ws(_e):
        pass

    async def _first() -> dict:
        return await tm._drive_cli_run(
            prompt="fill context",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-codex-usage-1",
            lifecycle_message_id="lifecycle-codex-usage-1",
        )

    async def _second() -> dict:
        return await tm._drive_cli_run(
            prompt="next turn should preempt",
            cwd="/tmp",
            model="sonnet",
            session_id="old-provider-usage",
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-codex-usage-2",
            lifecycle_message_id="lifecycle-codex-usage-2",
        )

    async def _go() -> tuple[dict, dict, dict]:
        await _start_drive_lifecycle(
            tm,
            session_id=sid,
            turn_run_id="turn-codex-usage-1",
            lifecycle_message_id="lifecycle-codex-usage-1",
            assistant_message_id="lifecycle-codex-usage-1-assistant",
        )
        first = await _first()
        after_first = session_manager.get(sid) or {}
        await _finish_drive_lifecycle(tm, sid)
        await _start_drive_lifecycle(
            tm,
            session_id=sid,
            turn_run_id="turn-codex-usage-2",
            lifecycle_message_id="lifecycle-codex-usage-2",
            assistant_message_id="lifecycle-codex-usage-2-assistant",
        )
        try:
            second = await _second()
        finally:
            await _finish_drive_lifecycle(tm, sid)
        return first, after_first, second

    first, after_first, second = _run_with_lifecycle(tm, _go)
    fresh = session_manager.get(sid) or {}
    check("first result success", first.get("success") is True)
    check("context window persisted", after_first.get("context_window") == 1000)
    check("context tokens persisted", after_first.get("context_tokens") == 950)
    check("provider spawned twice total", len(provider.prompts) == 2)
    check("second real spawn is fresh", provider.session_ids[-1] is None)
    check("chain persisted usage provider sid", fresh.get("continuation_chain") == ["old-provider-usage"])
    check("runner received usage continuation chain", provider.continuation_chains[-1] == ["old-provider-usage"])
    check("second result success", second.get("success") is True)


def test_lazy_selector_change_continuation() -> None:
    print("T7d lazy selector change continuation")
    provider_a_id = config_store.get_default_provider()["id"]
    provider_b_id = config_store.add_provider({
        "name": "Provider B",
        "kind": "claude",
        "mode": "subscription",
        "default_model": "haiku",
        "custom_models": ["haiku"],
    })["id"]
    session = session_manager.create(
        name="lazy-selector",
        cwd="/tmp",
        model="sonnet",
        provider_id=provider_a_id,
    )
    sid = session["id"]

    # Simulate a successful previous run before a lifecycle-owned selector handoff.
    session_manager.set_agent_sid(
        sid,
        "native",
        "old-provider-sid",
        provider_id=provider_a_id,
        model="sonnet",
    )

    # Change the projected selectors; the lifecycle authority supplies the
    # durable handoff read by TurnManager.
    session_manager.set_selectors(sid, provider_id=provider_b_id, model="haiku")

    provider = _RetryProvider(
        [{
            "success": True,
            "session_id": "fresh-provider-b",
            "token_usage": {"input_tokens": 1},
        }],
        id=provider_b_id,
        kind="claude",
        emit_discovered=True,
    )
    c = _StubCoordinator()
    c.lifecycle_commands.handoffs[f"{sid}:primary"] = (
        "old-provider-sid",
        SelectorIdentity(provider_b_id, "haiku", "native"),
    )
    c.provider_for_session = lambda _sid: provider
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)

    async def _ws(_e):
        pass

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt="continue on new model",
            cwd="/tmp",
            model="haiku",
            session_id="old-provider-sid",
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-lazy-selector",
            lifecycle_message_id="lifecycle-lazy-selector",
        )

    result = _run_drive_with_lifecycle(
        tm,
        _go,
        session_id=sid,
        turn_run_id="turn-lazy-selector",
        lifecycle_message_id="lifecycle-lazy-selector",
    )
    fresh = session_manager.get(sid) or {}
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check(
        "chain persisted old provider sid",
        fresh.get("continuation_chain") == ["old-provider-sid"],
    )
    check("runner received continuation chain", provider.continuation_chains == [["old-provider-sid"]])
    check("prompt contains provider change msg", "Session provider or model changed" in provider.prompts[0])
    check("last active provider updated", fresh.get("last_active_provider_id") == provider_b_id)
    check("last active model updated", fresh.get("last_active_model") == "haiku")
    check("result success", result.get("success") is True)


def test_s1_never_consumes_selector_handoff() -> None:
    source = (Path(__file__).resolve().parents[1] / "turn_manager.py").read_text(
        encoding="utf-8"
    )
    check(
        "consume_selector_handoff(" not in source,
        "S1 leaves selector handoff pending for S2 durable acceptance",
    )
    check(
        "execution.artifact.runtime_policy.get(" in source
        and "admit_prepared_selector_attempt(" in source,
        "prepared E2 compatibility binds at provider-attempt election",
    )
    check(
        "provider.discard_prepared_execution" in source,
        "unregistered prepared attempts release provider staging",
    )


def test_run_turn_persists_and_consumes_elected_selector_slot() -> None:
    compatibility = {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "primary",
        "thread_store_root": "/tmp/claude/projects",
        "claude_project_namespace": "-tmp",
    }

    class _RunTurnUPM:
        def __init__(self) -> None:
            self.lifecycle_message_id = ""

        def get_in_flight_lifecycle_msg_id(self, _sid):
            return self.lifecycle_message_id

        async def notify_user_msg_persisted(self, *_args) -> None:
            return None

    class _Provider:
        KIND = "claude"
        id = "run-turn-slot-provider"
        record = {"name": "Run turn slot provider"}

    class _RunTurnCoordinator(_StubCoordinator):
        def __init__(self, engine: LifecycleCommandEngine) -> None:
            super().__init__()
            self.lifecycle_commands = engine
            self.user_prompt_manager = _RunTurnUPM()
            self._session_cancelled = {}

        async def _init_turn_messages(self, **kwargs):
            message = {
                "id": self.user_prompt_manager.lifecycle_message_id,
                "role": "user",
                "content": kwargs["prompt"],
                "events": [],
            }
            session_manager.append_user_msg(
                kwargs["app_session_id"],
                message,
                strict=True,
            )
            return message

        def _build_assistant_msg(self, **kwargs):
            return {
                "id": f'assistant-{kwargs["app_session_id"]}',
                "role": "assistant",
                "content": "",
                "events": [],
                "workers": [],
            }

        async def _dispatch_messages_delta(self, *_args, **_kwargs) -> None:
            return None

        def _finalize_turn_messages(self, **_kwargs) -> None:
            return None

        def _forget_active_prompt_item(self, _item_id) -> None:
            return None

        async def await_outstanding_mssg(self, _sid) -> None:
            return None

        def provider_for_run(self, *_args, **_kwargs):
            return _Provider()

    async def _go() -> None:
        engine = LifecycleCommandEngine(EventBus())
        await engine.bind()
        try:
            for selector_role, session_id_field, source in (
                ("primary", "agent_session_id", "supervisor"),
                ("supervisor", "supervisor_agent_session_id", None),
            ):
                session = session_manager.create(
                    name=f"run-turn-slot-{selector_role}",
                    cwd="/tmp",
                    model="sonnet",
                    orchestration_mode="native",
                )
                sid = session["id"]
                lifecycle_message_id = f"run-turn-message-{selector_role}"
                await engine.begin_turn(
                    request_id=f"run-turn-begin-{selector_role}",
                    session_id=sid,
                    identity=UserTurnIdentity(
                        f"run-turn-user-{selector_role}",
                        lifecycle_message_id,
                    ),
                    execution_policy="sequential",
                )
                target = SelectorIdentity(
                    str(session.get("provider_id") or "claude"),
                    str(session.get("model") or "sonnet"),
                    str(session.get("runner") or ""),
                )
                source_selector = SelectorIdentity(
                    "source-provider",
                    "source-model",
                    "source-runner",
                )
                lifecycle_command_store.persist_admitted_selector_attempt(
                    sid,
                    target=source_selector,
                    native_sid_compatibility=compatibility,
                    primary_native_sid=f"primary-source-{selector_role}",
                    supervisor_native_sid=f"supervisor-source-{selector_role}",
                    primary_native_sid_compatibility=compatibility,
                    supervisor_native_sid_compatibility=compatibility,
                )
                lifecycle_command_store.persist_selector_transition(
                    sid,
                    target=target,
                    projection_updates=target.to_dict(),
                    primary_native_sid=None,
                    supervisor_native_sid=None,
                    primary_legacy_native_sid_compatibility=None,
                    supervisor_legacy_native_sid_compatibility=None,
                )
                assert lifecycle_command_store.acknowledge_selector_projection(
                    sid,
                    target,
                )
                authority, decision = (
                    lifecycle_command_store.persist_admitted_selector_attempt(
                        sid,
                        target=target,
                        native_sid_compatibility=compatibility,
                        primary_native_sid=None,
                        supervisor_native_sid=None,
                        primary_native_sid_compatibility=None,
                        supervisor_native_sid_compatibility=None,
                    )
                )
                assert decision == "admitted"

                coordinator = _RunTurnCoordinator(engine)
                coordinator.user_prompt_manager.lifecycle_message_id = (
                    lifecycle_message_id
                )
                manager = TurnManager(coordinator)
                await manager.lifecycle.bind()
                captured_execution_ids: list[str] = []

                async def _drive_cli_run(**kwargs):
                    execution = engine.snapshot(sid).execution
                    assert execution is not None
                    captured_execution_ids.append(
                        execution.identity.execution_turn_id
                    )
                    provider_run_id = f"provider-run-{selector_role}"
                    await engine.elect_execution_attempt(
                        sid,
                        execution_identity=execution.identity,
                        provider_run_id=provider_run_id,
                        selector_generation=authority.generation,
                        native_sid_compatibility=compatibility,
                        selector=target,
                    )
                    assert await engine.attach_selector_native_sid(
                        sid,
                        selector_generation=authority.generation,
                        provider_run_id=provider_run_id,
                        role=selector_role,
                        native_sid=f"target-{selector_role}",
                    )
                    return {
                        "success": True,
                        "session_id": f"target-{selector_role}",
                        "events": [],
                        "provider_kind": "claude",
                    }

                async def _noop(*_args, **_kwargs) -> None:
                    return None

                manager._drive_cli_run = _drive_cli_run
                manager._publish_turn_start_lifecycle = _noop
                manager._publish_terminal_lifecycle = _noop
                manager._await_worker_panel_fold = _noop
                await manager.run_turn(
                    session=session,
                    prompt="continue",
                    cli_prompt="continue",
                    app_session_id=sid,
                    model="sonnet",
                    cwd="/tmp",
                    ws_callback=_noop,
                    images=None,
                    trace_step_name="run-turn-slot-test",
                    session_id_field=session_id_field,
                    mode="native",
                    source=source,
                )
                execution_turn_id = captured_execution_ids[0]
                assert lifecycle_command_store.execution_selector_role(
                    sid,
                    execution_turn_id,
                ) == selector_role
                final_authority = (
                    lifecycle_command_store.selector_authority_snapshot(sid)
                )
                assert final_authority is not None
                assert final_authority.handoff is not None
                assert getattr(
                    final_authority.handoff,
                    f"{selector_role}_source_sid",
                ) is None
                other_role = (
                    "supervisor" if selector_role == "primary" else "primary"
                )
                assert getattr(
                    final_authority.handoff,
                    f"{other_role}_source_sid",
                ) is not None
                await manager.lifecycle.close()
        finally:
            await engine.close()

    asyncio.run(_go())


def test_stale_prepared_selector_attempt_is_discarded_and_retried() -> None:
    provider_id = config_store.get_default_provider()["id"]
    session = session_manager.create(
        name="stale-selector-attempt",
        cwd="/tmp",
        model="sonnet",
        provider_id=provider_id,
    )
    sid = session["id"]
    provider = _RetryProvider(
        [{
            "success": True,
            "session_id": "current-selector-sid",
            "token_usage": {"input_tokens": 1},
        }],
        id=provider_id,
        kind="claude",
        emit_discovered=True,
    )
    coordinator = _StubCoordinator()
    coordinator.lifecycle_commands.selector_attempt_decisions = [
        "stale",
        "admitted",
    ]
    coordinator.provider_for_session = lambda _sid: provider
    coordinator.user_prompt_manager = _UPM()
    manager = TurnManager(coordinator)

    async def _ws(_event):
        return None

    async def _go() -> dict:
        return await manager._drive_cli_run(
            prompt="continue",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-stale-selector",
            lifecycle_message_id="lifecycle-stale-selector",
        )

    result = _run_drive_with_lifecycle(
        manager,
        _go,
        session_id=sid,
        turn_run_id="turn-stale-selector",
        lifecycle_message_id="lifecycle-stale-selector",
    )
    check("stale attempt was prepared", provider.prepared == 2)
    check("stale attempt released provider staging", provider.discarded == 1)
    check("only current selector attempt launched", len(provider.prompts) == 1)
    check("stale attempt retried without crashing", result.get("success") is True)


def test_actual_two_turn_discovery_reuses_better_agent_and_remote_sid() -> None:
    async def _go() -> None:
        engine = LifecycleCommandEngine(EventBus())
        turn_lifecycles = []
        try:
            for label, runner, compatibility in (
                (
                    "better-agent-runner",
                    "better_agent_runner",
                    {
                        "schema": 1,
                        "engine": "better-agent-runner",
                        "node_id": "primary",
                        "thread_store_root": "/tmp/better_agent_sessions",
                        "claude_project_namespace": None,
                    },
                ),
                (
                    "remote-node",
                    "native",
                    {
                        "schema": 1,
                        "engine": "claude-native",
                        "node_id": "worker-remote",
                        "thread_store_root": r"C:\\Users\\agent\\.claude\\projects",
                        "claude_project_namespace": "-remote-work",
                    },
                ),
            ):
                provider_id = config_store.get_default_provider()["id"]
                session = session_manager.create(
                    name=f"two-turn-{label}",
                    cwd="/tmp",
                    model="sonnet",
                    provider_id=provider_id,
                    orchestration_mode="native",
                )
                sid = session["id"]
                session_manager.set_selectors(sid, runner=runner)
                native_sid = f"{label}-sid"
                provider = _RetryProvider(
                    [
                        {
                            "success": True,
                            "session_id": native_sid,
                            "token_usage": {"input_tokens": 1},
                        },
                        {
                            "success": True,
                            "session_id": native_sid,
                            "token_usage": {"input_tokens": 1},
                        },
                    ],
                    id=provider_id,
                    kind="claude",
                    emit_discovered=True,
                    runner=runner,
                    native_sid_compatibility=compatibility,
                    compatibility_mismatches=(1 if label == "remote-node" else 0),
                )
                coordinator = _StubCoordinator()
                coordinator.lifecycle_commands = engine
                coordinator.provider_for_session = lambda _sid, p=provider: p
                coordinator.user_prompt_manager = _UPM()
                manager = TurnManager(coordinator)
                await manager.lifecycle.bind()
                turn_lifecycles.append(manager.lifecycle)
                turn_identity = UserTurnIdentity(
                    f"two-turn-user-{label}",
                    f"two-turn-message-{label}",
                )
                await engine.begin_turn(
                    request_id=f"two-turn-begin-{label}",
                    session_id=sid,
                    identity=turn_identity,
                    execution_policy="sequential",
                )

                async def _ws(_event):
                    return None

                for index in range(2):
                    execution_identity = ExecutionTurnIdentity(
                        f"two-turn-execution-{label}-{index}",
                        f"two-turn-assistant-{label}-{index}",
                        "native",
                    )
                    await engine.start_execution(
                        sid,
                        execution_identity=execution_identity,
                        selector_role="primary",
                    )
                    current_sid = (
                        (session_manager.get(sid) or {}).get("agent_session_id")
                        if index
                        else None
                    )
                    await _start_drive_lifecycle(
                        manager,
                        session_id=sid,
                        turn_run_id=f"two-turn-run-{label}-{index}",
                        lifecycle_message_id=turn_identity.lifecycle_message_id,
                        assistant_message_id=execution_identity.assistant_message_id,
                    )
                    result = await manager._drive_cli_run(
                        prompt="continue",
                        cwd="/tmp",
                        model="sonnet",
                        session_id=current_sid,
                        ws_callback=_ws,
                        app_session_id=sid,
                        cancel_event=asyncio.Event(),
                        session_id_field="agent_session_id",
                        mode="native",
                        turn_run_id=f"two-turn-run-{label}-{index}",
                        lifecycle_message_id=turn_identity.lifecycle_message_id,
                        execution_identity=execution_identity,
                    )
                    await _finish_drive_lifecycle(manager, sid)
                    check(f"{label} turn {index + 1} succeeded", result["success"])
                    assert result["success"]
                    execution = engine.snapshot(sid).execution
                    assert execution is not None
                    assert execution.provider_run_id is not None
                    await engine.finish_execution(
                        sid,
                        execution_identity=execution_identity,
                        provider_run_id=execution.provider_run_id,
                        outcome="complete",
                    )
                authority = lifecycle_command_store.selector_authority_snapshot(sid)
                check(
                    f"{label} production discovery attached SID",
                    authority is not None
                    and authority.primary_native_sid == native_sid
                    and authority.primary_native_sid_compatibility == compatibility,
                )
                assert authority is not None
                assert authority.primary_native_sid == native_sid
                assert authority.primary_native_sid_compatibility == compatibility
                check(
                    f"{label} second turn reused discovered SID",
                    provider.session_ids == [None, native_sid],
                )
                assert provider.session_ids == [None, native_sid]
                if label == "remote-node":
                    assert provider.prepared == 3
                await engine.finish_active(
                    sid,
                    lifecycle_message_id=turn_identity.lifecycle_message_id,
                    outcome="complete",
                )
                await manager.lifecycle.close()
                turn_lifecycles.remove(manager.lifecycle)
        finally:
            for lifecycle in turn_lifecycles:
                await lifecycle.close()
            await engine.close()

    asyncio.run(_go())


def test_selector_changes_fence_production_sid_discovery_and_completion() -> None:
    async def _go() -> None:
        engine = LifecycleCommandEngine(EventBus())
        turn_lifecycles = []
        try:
            for phase in ("before-discovery", "after-discovery"):
                provider_id = config_store.get_default_provider()["id"]
                session = session_manager.create(
                    name=f"selector-race-{phase}",
                    cwd="/tmp",
                    model="sonnet",
                    provider_id=provider_id,
                    orchestration_mode="native",
                )
                sid = session["id"]
                native_sid = f"stale-{phase}-sid"

                async def switch_selector() -> None:
                    await engine.transition_selectors(
                        sid,
                        updates={"model": "haiku"},
                    )

                provider = _RetryProvider(
                    [{
                        "success": True,
                        "session_id": native_sid,
                        "token_usage": {"input_tokens": 1},
                    }],
                    id=provider_id,
                    kind="claude",
                    emit_discovered=True,
                    before_discovery=(
                        switch_selector if phase == "before-discovery" else None
                    ),
                )
                coordinator = _StubCoordinator()
                coordinator.lifecycle_commands = engine
                coordinator.provider_for_session = lambda _sid, p=provider: p
                coordinator.user_prompt_manager = _UPM()
                manager = TurnManager(coordinator)
                await manager.lifecycle.bind()
                turn_lifecycles.append(manager.lifecycle)
                turn_identity = UserTurnIdentity(
                    f"selector-race-user-{phase}",
                    f"selector-race-message-{phase}",
                )
                execution_identity = ExecutionTurnIdentity(
                    f"selector-race-execution-{phase}",
                    f"selector-race-assistant-{phase}",
                    "native",
                )
                await engine.begin_turn(
                    request_id=f"selector-race-begin-{phase}",
                    session_id=sid,
                    identity=turn_identity,
                    execution_policy="sequential",
                )
                await engine.start_execution(
                    sid,
                    execution_identity=execution_identity,
                    selector_role="primary",
                )

                original_attach = engine.attach_selector_native_sid
                switched_after_attach = False
                if phase == "after-discovery":
                    async def attach_then_switch(*args, **kwargs) -> bool:
                        nonlocal switched_after_attach
                        attached = await original_attach(*args, **kwargs)
                        if attached and not switched_after_attach:
                            switched_after_attach = True
                            await switch_selector()
                        return attached

                    engine.attach_selector_native_sid = attach_then_switch

                async def _ws(_event):
                    return None

                try:
                    await _start_drive_lifecycle(
                        manager,
                        session_id=sid,
                        turn_run_id=f"selector-race-run-{phase}",
                        lifecycle_message_id=turn_identity.lifecycle_message_id,
                        assistant_message_id=execution_identity.assistant_message_id,
                    )
                    result = await manager._drive_cli_run(
                        prompt="continue",
                        cwd="/tmp",
                        model="sonnet",
                        session_id=None,
                        ws_callback=_ws,
                        app_session_id=sid,
                        cancel_event=asyncio.Event(),
                        session_id_field="agent_session_id",
                        mode="native",
                        turn_run_id=f"selector-race-run-{phase}",
                        lifecycle_message_id=turn_identity.lifecycle_message_id,
                        execution_identity=execution_identity,
                    )
                finally:
                    engine.attach_selector_native_sid = original_attach
                await _finish_drive_lifecycle(manager, sid)

                authority = lifecycle_command_store.selector_authority_snapshot(sid)
                current = session_manager.get(sid) or {}
                check(f"{phase} run completed", result["success"] is True)
                check(
                    f"{phase} stale SID absent from lifecycle authority",
                    authority is not None and authority.primary_native_sid is None,
                )
                check(
                    f"{phase} stale SID absent from session projection",
                    current.get("agent_session_id") is None,
                )
                assert authority is not None
                assert authority.primary_native_sid is None
                assert current.get("agent_session_id") is None
                if phase == "after-discovery":
                    check(
                        "selector handoff remains pending after completion",
                        authority.handoff is not None
                        and authority.handoff.primary_source_sid == native_sid,
                    )
                    assert authority.handoff is not None
                    assert authority.handoff.primary_source_sid == native_sid

                execution = engine.snapshot(sid).execution
                assert execution is not None
                assert execution.provider_run_id is not None
                await engine.finish_execution(
                    sid,
                    execution_identity=execution_identity,
                    provider_run_id=execution.provider_run_id,
                    outcome="complete",
                )
                await engine.finish_active(
                    sid,
                    lifecycle_message_id=turn_identity.lifecycle_message_id,
                    outcome="complete",
                )
                await manager.lifecycle.close()
                turn_lifecycles.remove(manager.lifecycle)
        finally:
            for lifecycle in turn_lifecycles:
                await lifecycle.close()
            await engine.close()

    asyncio.run(_go())


def test_stale_session_error_clears_authority_before_selector_switch() -> None:
    async def _go() -> None:
        engine = LifecycleCommandEngine(EventBus())
        manager = None
        try:
            provider_id = config_store.get_default_provider()["id"]
            session = session_manager.create(
                name="stale-session-authority-clear",
                cwd="/tmp",
                model="sonnet",
                provider_id=provider_id,
                orchestration_mode="native",
            )
            sid = session["id"]
            native_sid = "provider-proven-invalid-sid"
            provider = _RetryProvider(
                [
                    {
                        "success": True,
                        "session_id": native_sid,
                        "token_usage": {"input_tokens": 1},
                    },
                    {
                        "success": False,
                        "session_id": native_sid,
                        "error": "session not found",
                        "token_usage": None,
                    },
                ],
                id=provider_id,
                kind="claude",
                emit_discovered=True,
            )
            coordinator = _StubCoordinator()
            coordinator.lifecycle_commands = engine
            coordinator.provider_for_session = lambda _sid: provider
            coordinator.user_prompt_manager = _UPM()
            manager = TurnManager(coordinator)
            await manager.lifecycle.bind()
            turn_identity = UserTurnIdentity(
                "stale-session-user",
                "stale-session-message",
            )
            await engine.begin_turn(
                request_id="stale-session-begin",
                session_id=sid,
                identity=turn_identity,
                execution_policy="sequential",
            )

            async def _ws(_event):
                return None

            first_execution = ExecutionTurnIdentity(
                "stale-session-execution-1",
                "stale-session-assistant-1",
                "native",
            )
            await engine.start_execution(
                sid,
                execution_identity=first_execution,
                selector_role="primary",
            )
            await _start_drive_lifecycle(
                manager,
                session_id=sid,
                turn_run_id="stale-session-run-1",
                lifecycle_message_id=turn_identity.lifecycle_message_id,
                assistant_message_id=first_execution.assistant_message_id,
            )
            first = await manager._drive_cli_run(
                prompt="start",
                cwd="/tmp",
                model="sonnet",
                session_id=None,
                ws_callback=_ws,
                app_session_id=sid,
                cancel_event=asyncio.Event(),
                session_id_field="agent_session_id",
                mode="native",
                turn_run_id="stale-session-run-1",
                lifecycle_message_id=turn_identity.lifecycle_message_id,
                execution_identity=first_execution,
            )
            await _finish_drive_lifecycle(manager, sid)
            assert first["success"] is True
            first_snapshot = engine.snapshot(sid).execution
            assert first_snapshot is not None
            assert first_snapshot.provider_run_id is not None
            await engine.finish_execution(
                sid,
                execution_identity=first_execution,
                provider_run_id=first_snapshot.provider_run_id,
                outcome="complete",
            )

            second_execution = ExecutionTurnIdentity(
                "stale-session-execution-2",
                "stale-session-assistant-2",
                "native",
            )
            await engine.start_execution(
                sid,
                execution_identity=second_execution,
                selector_role="primary",
            )
            await _start_drive_lifecycle(
                manager,
                session_id=sid,
                turn_run_id="stale-session-run-2",
                lifecycle_message_id=turn_identity.lifecycle_message_id,
                assistant_message_id=second_execution.assistant_message_id,
            )
            second = await manager._drive_cli_run(
                prompt="resume",
                cwd="/tmp",
                model="sonnet",
                session_id=native_sid,
                ws_callback=_ws,
                app_session_id=sid,
                cancel_event=asyncio.Event(),
                session_id_field="agent_session_id",
                mode="native",
                turn_run_id="stale-session-run-2",
                lifecycle_message_id=turn_identity.lifecycle_message_id,
                execution_identity=second_execution,
            )
            await _finish_drive_lifecycle(manager, sid)
            assert second["success"] is False
            cleared = lifecycle_command_store.selector_authority_snapshot(sid)
            assert cleared is not None
            check(
                "stale error clears lifecycle and session SID together",
                cleared.primary_native_sid is None
                and (session_manager.get(sid) or {}).get("agent_session_id") is None,
            )
            assert cleared.primary_native_sid is None
            assert (session_manager.get(sid) or {}).get("agent_session_id") is None

            await engine.transition_selectors(sid, updates={"model": "haiku"})
            transitioned = lifecycle_command_store.selector_authority_snapshot(sid)
            check(
                "selector switch cannot hand off provider-proven-invalid SID",
                transitioned is not None and transitioned.handoff is None,
            )
            assert transitioned is not None
            assert transitioned.handoff is None

            second_snapshot = engine.snapshot(sid).execution
            assert second_snapshot is not None
            assert second_snapshot.provider_run_id is not None
            await engine.finish_execution(
                sid,
                execution_identity=second_execution,
                provider_run_id=second_snapshot.provider_run_id,
                outcome="failed",
            )
            await engine.finish_active(
                sid,
                lifecycle_message_id=turn_identity.lifecycle_message_id,
                outcome="failed",
            )
        finally:
            if manager is not None:
                await manager.lifecycle.close()
            await engine.close()

    asyncio.run(_go())


def test_wait_for_clear_runs_blocks_then_releases() -> None:
    print("T8 wait_for_clear_runs barrier")
    tm = TurnManager(_StubCoordinator())
    sid = "sid-6"
    tm.active_run_ids[sid] = ["run-x"]

    async def _go() -> tuple[bool, bool]:
        waiter = asyncio.create_task(tm.wait_for_clear_runs(sid))
        await asyncio.sleep(1.2)
        blocked = not waiter.done()
        tm.active_run_ids.pop(sid)
        await asyncio.wait_for(waiter, timeout=3)
        return blocked, True

    blocked, released = asyncio.run(_go())
    check("blocked while run registered", blocked)
    check("released when cleared", released)


def test_stop_terminally_cancels_exact_prompt_blocked_before_begin() -> None:
    print("T9 stop terminally cancels exact prompt blocked before begin")
    coordinator = Coordinator()
    sid = session_manager.create(
        name="pre-begin-cancel",
        cwd="/tmp",
        model="sonnet",
        orchestration_mode="native",
    )["id"]
    queued_id = "queue-pre-begin"
    lifecycle_msg_id = "lifecycle-pre-begin"
    queued_prompt = {
        "id": queued_id,
        "content": "must never reach provider",
        "lifecycle_msg_id": lifecycle_msg_id,
        "client_id": "client-pre-begin",
        "kind": "send",
    }
    sibling_id = "queue-sibling"
    sibling_prompt = {
        "id": sibling_id,
        "content": "must remain queued",
        "lifecycle_msg_id": "lifecycle-sibling",
        "client_id": "client-sibling",
        "kind": "queued_behind",
    }
    session_manager.add_queued_prompt(sid, queued_prompt)
    session_manager.add_queued_prompt(sid, sibling_prompt)
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({
        "_queued_id": queued_id,
        "prompt": queued_prompt["content"],
        "lifecycle_msg_id": lifecycle_msg_id,
        "client_id": queued_prompt["client_id"],
    })
    q.put_nowait(None)
    q.put_nowait({
        "_queued_id": sibling_id,
        "prompt": sibling_prompt["content"],
        "lifecycle_msg_id": sibling_prompt["lifecycle_msg_id"],
        "client_id": sibling_prompt["client_id"],
    })
    coordinator._prompt_queues[sid] = q
    coordinator._queued_ids[sid] = [queued_id, sibling_id]
    coordinator.begin_queued_edit(sid, queued_id)

    barrier_entered = asyncio.Event()
    original_gate = coordinator._defer_if_queued_editing

    async def observed_gate(*args, **kwargs):
        barrier_entered.set()
        return await original_gate(*args, **kwargs)

    coordinator._defer_if_queued_editing = observed_gate
    terminal_events = []
    terminal_arrived = asyncio.Event()

    async def capture_terminal(event) -> None:
        if event.sid == sid and event.msg_id == lifecycle_msg_id:
            terminal_events.append(event)
            terminal_arrived.set()

    bus.subscribe(
        "user_message_failed",
        capture_terminal,
        name="test_pre_begin_cancel_terminal",
    )

    async def _go() -> tuple[list[dict], list[dict], list[dict]]:
        from ws_chat import _handle_stop_message

        stop_frames: list[dict] = []

        async def capture_stop_frame(frame: dict) -> None:
            stop_frames.append(frame)

        processor = asyncio.create_task(coordinator._run_session_processor(sid))
        try:
            try:
                await asyncio.wait_for(barrier_entered.wait(), timeout=2.0)
                await asyncio.wait_for(
                    _handle_stop_message(coordinator, sid, capture_stop_frame),
                    timeout=2.0,
                )
                await asyncio.wait_for(terminal_arrived.wait(), timeout=2.0)
            except BaseException:
                processor.cancel()
                await asyncio.gather(processor, return_exceptions=True)
                raise
        finally:
            coordinator.finish_queued_edit(sid, queued_id)
            try:
                if not processor.done():
                    try:
                        await asyncio.wait_for(processor, timeout=2.0)
                    except TimeoutError:
                        processor.cancel()
                        await asyncio.gather(processor, return_exceptions=True)
                        raise
            finally:
                await asyncio.wait_for(
                    coordinator.turn_manager.lifecycle.close(),
                    timeout=2.0,
                )
                await asyncio.wait_for(
                    coordinator.lifecycle_commands.close(),
                    timeout=2.0,
                )
        durable_queue = list(
            (session_manager.get(sid) or {}).get("queued_prompts") or []
        )
        remaining_queue = []
        while not q.empty():
            remaining_queue.append(q.get_nowait())
        return stop_frames, durable_queue, remaining_queue

    try:
        stop_frames, durable_queue, remaining_queue = asyncio.run(_go())
    finally:
        bus.unsubscribe("test_pre_begin_cancel_terminal")

    check(
        "successful stop acknowledged",
        stop_frames == [{
            "type": "stop_acknowledged",
            "data": {"app_session_id": sid, "success": True},
        }],
    )
    check(
        "only exact durable queue row removed",
        [item.get("id") for item in durable_queue] == [sibling_id],
    )
    check(
        "sibling in-memory identity retained",
        coordinator._queued_ids.get(sid) == [sibling_id],
    )
    check("cancel emitted exactly one terminal", len(terminal_events) == 1)
    check(
        "terminal reason is aborted_before_send",
        bool(terminal_events)
        and terminal_events[0].payload.get("reason") == "aborted_before_send",
    )
    check(
        "provider-facing user message was never persisted",
        (session_manager.get(sid) or {}).get("messages") == [],
    )
    check(
        "only sibling remains in processor queue",
        [item.get("_queued_id") for item in remaining_queue] == [sibling_id],
    )
    check("in-flight counter cleared", sid not in coordinator._in_flight_prompts)
    check("pre-begin owner cleared", not coordinator.turn_manager.has_pre_begin_prompt(sid))
    check("session cancel flag cleared", sid not in coordinator._session_cancelled)


def test_submit_prompt_requests_recovery_before_enqueue() -> None:
    import recovery

    coordinator = Coordinator.__new__(Coordinator)
    calls: list[tuple[str, str]] = []
    original_request = recovery.request_recovered_session

    async def request_recovery(session_id: str) -> None:
        calls.append(("recovery", session_id))

    def enqueue(session_id: str, _params: dict, **_kwargs) -> str:
        calls.append(("enqueue", session_id))
        return "queued"

    recovery.request_recovered_session = request_recovery
    coordinator.submit_prompt = enqueue
    try:
        queued_id = asyncio.run(coordinator.submit_prompt_async(
            "sid-demand",
            {"prompt": "continue"},
        ))
    finally:
        recovery.request_recovered_session = original_request

    assert queued_id == "queued"
    assert calls == [
        ("recovery", "sid-demand"),
        ("enqueue", "sid-demand"),
    ]


def main() -> int:
    test_noop_cancel_does_not_leak_session_cancelled()
    test_gap_cancel_signals_exact_pre_begin()
    test_cancel_fans_out_to_recovered_runs()
    test_recovered_cancel_escalates_when_still_alive()
    test_evict_stale_skips_alive_pid()
    test_prune_dead_run_clears_active_run_id()
    test_prune_stale_pidless_run_clears_active_run_id()
    test_prune_stale_retrying_pidless_run_is_retained()
    test_codex_initialize_timeout_is_not_transient()
    test_drive_cli_run_pre_spawn_guard()
    test_native_non_user_turn_gets_loopback_credentials()
    test_drive_cli_run_flushes_target_before_spawn()
    test_rate_limit_wait_keeps_turn_active_and_cancellable()
    test_rate_limit_retry_spawns_once_then_emits_terminal()
    test_rate_limit_wait_uses_reset_or_one_minute_fallback()
    test_rate_limit_wait_can_continue_immediately()
    test_forced_context_overflow_retries_as_fresh_continuation()
    test_capability_change_retries_as_fresh_continuation()
    test_context_continuation_start_runs_off_loop()
    test_selector_native_sid_projection_runs_off_loop()
    test_codex_context_fill_preempts_native_compaction()
    test_codex_context_usage_persists_then_preempts_next_turn()
    test_lazy_selector_change_continuation()
    test_s1_never_consumes_selector_handoff()
    test_stale_prepared_selector_attempt_is_discarded_and_retried()
    test_actual_two_turn_discovery_reuses_better_agent_and_remote_sid()
    test_selector_changes_fence_production_sid_discovery_and_completion()
    test_stale_session_error_clears_authority_before_selector_switch()
    test_wait_for_clear_runs_blocks_then_releases()
    test_stop_terminally_cancels_exact_prompt_blocked_before_begin()
    test_submit_prompt_requests_recovery_before_enqueue()
    print()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
