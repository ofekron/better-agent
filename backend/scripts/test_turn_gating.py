"""Turn-gating regression tests for the dequeue→cancel_events gap and
recovery-overlap findings.

Locks:
  1. cancel_turn with NO live turn and nothing in flight is a pure
     no-op: returns False and does NOT set _session_cancelled (the
     old unconditional set leaked into the next turn and suppressed
     its supervisor verdict).
  2. cancel_turn with NO live turn but a dequeued prompt mid-gap
     (_in_flight_prompts > 0) parks a pending cancel and reports True.
  3. cancel_turn with NO live turn but registered active_run_ids
     (recovered runs) fans the cancel out to them and reports True.
  4. _evict_stale_runs never evicts an entry whose pid is alive.
  5. _drive_cli_run refuses to spawn when its cancel_event is already
     set (pending-cancel displaced the turn before spawn).
  6. wait_for_clear_runs blocks while active_run_ids is non-empty and
     releases when it clears.

Run with:
    cd backend && .venv/bin/python scripts/test_turn_gating.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_gating_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from turn_helpers import _is_transient_error  # noqa: E402
from turn_manager import TurnManager  # noqa: E402
import turn_manager as turn_manager_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import user_prefs  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


class _StubCoordinator:
    def __init__(self) -> None:
        self._in_flight_prompts: dict[str, int] = {}
        self._prompt_queues: dict = {}
        self._session_cancelled: dict[str, bool] = {}
        self.fanned_out: list[str] = []
        self.hard_cancelled: list[str] = []
        self.internal_token = "test-token"

    def _cancel_turn_fanout(self, run_id: str) -> bool:
        self.fanned_out.append(run_id)
        return True

    def _cancel_recovered_run_fanout(self, run_id: str) -> bool:
        self.hard_cancelled.append(run_id)
        return True

    async def broadcast_session(
        self, *args, **kwargs,
    ) -> None:
        pass


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
        emit_discovered: bool = False,
        reset_seconds: float = 0.5,
    ) -> None:
        self._runs: dict = {}
        self.outcomes = outcomes
        self.prompts: list[str] = []
        self.session_ids: list[str | None] = []
        self.continuation_chains: list[list[str] | None] = []
        self.cancelled: list[str] = []
        self.KIND = "codex"
        self.id = id
        self._emit_discovered = emit_discovered
        self.reset_seconds = reset_seconds

    def start_run(self, **kw):
        self.prompts.append(kw["prompt"])
        self.session_ids.append(kw.get("session_id"))
        self.continuation_chains.append(kw.get("continuation_chain"))
        run_id = kw["run_id"]
        queue = kw["queue"]
        idx = len(self.prompts) - 1
        payload = self.outcomes[idx]
        self._runs[run_id] = type(
            "RunState",
            (),
            {"popen": type("Popen", (), {"pid": os.getpid()})()},
        )()

        if self._emit_discovered and payload.get("session_id"):
            kw["loop"].call_soon_threadsafe(
                queue.put_nowait,
                type("E", (), {
                    "type": "session_discovered",
                    "data": {"session_id": payload["session_id"]},
                })(),
            )
        kw["loop"].call_soon_threadsafe(
            queue.put_nowait,
            type("E", (), {"type": "complete", "data": payload})(),
        )

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
    ok = asyncio.run(tm.cancel_turn("sid-1"))
    check("returns False", ok is False)
    check("no _session_cancelled leak", "sid-1" not in c._session_cancelled)
    check("no pending parked", "sid-1" not in tm._pending_cancel)


def test_gap_cancel_parks_pending() -> None:
    print("T2 gap-window cancel parks pending cancel")
    c = _StubCoordinator()
    c._in_flight_prompts["sid-2"] = 1
    tm = TurnManager(c)
    ok = asyncio.run(tm.cancel_turn("sid-2", interrupted_by_msg_id="lm-9"))
    check("returns True", ok is True)
    check("pending parked with msg id", tm._pending_cancel.get("sid-2") == "lm-9")
    check("_session_cancelled set", c._session_cancelled.get("sid-2") is True)


def test_cancel_fans_out_to_recovered_runs() -> None:
    print("T3 cancel with no cancel_event fans out to active_run_ids")
    c = _StubCoordinator()
    tm = TurnManager(c)
    tm.active_run_ids["sid-3"] = ["run-r1", "run-r2"]
    ok = asyncio.run(tm.cancel_turn("sid-3"))
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
        asyncio.run(_go())
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


def test_drive_cli_run_pre_spawn_guard() -> None:
    print("T5 _drive_cli_run with pre-set cancel_event never spawns")
    c = _StubCoordinator()
    tm = TurnManager(c)
    spawned: list[str] = []

    class _Provider:
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
        )

    result = asyncio.run(_go())
    check("no spawn happened", spawned == [])
    check("result is cancelled-failure", result.get("success") is False)


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

    async def _go() -> tuple[bool, bool, bool, dict]:
        ev = asyncio.Event()
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
        ))
        await asyncio.sleep(0.2)
        no_terminal = not any(e.get("type") == "complete" for e in ws_events)
        active = bool(tm.active_run_ids.get("sid-rl-cancel"))
        pid_cleared = all(
            "pid" not in r for r in tm._run_state.get("sid-rl-cancel", [])
        )
        ev.set()
        result = await asyncio.wait_for(task, timeout=3)
        return no_terminal, active, pid_cleared, result

    no_terminal, active, pid_cleared, result = asyncio.run(_go())
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
        )

    result = asyncio.run(_go())
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
        ))
        await asyncio.sleep(0.2)
        landed = tm.request_immediate_continuation(
            sid,
            "p",
            reason="rate_limit_provider_switch",
        )
        result = await asyncio.wait_for(task, timeout=3)
        return {"landed": landed, **result}

    result = asyncio.run(_go())
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
        )

    result = asyncio.run(_go())
    fresh = session_manager.get(sid) or {}
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check("chain persisted old provider sid", fresh.get("continuation_chain") == ["old-provider"])
    check("runner received continuation chain", provider.continuation_chains == [["old-provider"]])
    check("prompt wrapped as continuation", "Previous provider session ids: old-provider" in provider.prompts[0])
    check("result success", result.get("success") is True)


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
        )

    result = asyncio.run(_go())
    fresh = session_manager.get(sid) or {}
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check(
        "chain persisted preempted provider sid",
        fresh.get("continuation_chain") == ["old-provider-preempt"],
    )
    check("runner received continuation chain", provider.continuation_chains == [["old-provider-preempt"]])
    check("result success", result.get("success") is True)


def test_lazy_selector_change_continuation() -> None:
    print("T7d lazy selector change continuation")
    session = session_manager.create(name="lazy-selector", cwd="/tmp", model="sonnet", provider_id="prov-a")
    sid = session["id"]

    # Simulate a successful previous run that set last_active_provider_id and last_active_model
    session_manager.set_agent_sid(sid, "native", "old-provider-sid", provider_id="prov-a", model="sonnet")

    # Change session selectors (provider/model) to simulate user action
    session_manager.set_selectors(sid, provider_id="prov-b", model="haiku")

    provider = _RetryProvider(
        [{
            "success": True,
            "session_id": "fresh-provider-b",
            "token_usage": {"input_tokens": 1},
        }],
        id="prov-b",
        emit_discovered=True,
    )
    c = _StubCoordinator()
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
        )

    result = asyncio.run(_go())
    fresh = session_manager.get(sid) or {}
    check("provider spawned once", len(provider.prompts) == 1)
    check("real spawn is fresh", provider.session_ids == [None])
    check(
        "chain persisted old provider sid",
        fresh.get("continuation_chain") == ["old-provider-sid"],
    )
    check("runner received continuation chain", provider.continuation_chains == [["old-provider-sid"]])
    check("prompt contains provider change msg", "Session provider or model changed" in provider.prompts[0])
    check("last active provider updated", fresh.get("last_active_provider_id") == "prov-b")
    check("last active model updated", fresh.get("last_active_model") == "haiku")
    check("result success", result.get("success") is True)


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


def main() -> int:
    test_noop_cancel_does_not_leak_session_cancelled()
    test_gap_cancel_parks_pending()
    test_cancel_fans_out_to_recovered_runs()
    test_recovered_cancel_escalates_when_still_alive()
    test_evict_stale_skips_alive_pid()
    test_prune_dead_run_clears_active_run_id()
    test_prune_stale_pidless_run_clears_active_run_id()
    test_prune_stale_retrying_pidless_run_is_retained()
    test_codex_initialize_timeout_is_not_transient()
    test_drive_cli_run_pre_spawn_guard()
    test_rate_limit_wait_keeps_turn_active_and_cancellable()
    test_rate_limit_retry_spawns_once_then_emits_terminal()
    test_rate_limit_wait_uses_reset_or_one_minute_fallback()
    test_rate_limit_wait_can_continue_immediately()
    test_forced_context_overflow_retries_as_fresh_continuation()
    test_codex_context_fill_preempts_native_compaction()
    test_lazy_selector_change_continuation()
    test_wait_for_clear_runs_blocks_then_releases()
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
