import contextlib
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_retarget_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_recovery  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402
from provider_openai import OpenAIProvider  # noqa: E402
from runs_dir import runs_root, atomic_write_json  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


class _FakeBatch:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSessionManager:
    def __init__(self, sess: dict) -> None:
        self.sess = sess
        self.agent_msg_ids: list[str] = []
        self.completed_msg_ids: list[str] = []

    def batch(self, sid: str, bump_updated_at: bool = False):
        return _FakeBatch()

    def get_ref(self, sid: str):
        return self.sess

    def _root_id_for(self, sid: str):
        return sid

    def set_agent_sid(self, *args, **kwargs):
        pass

    def set_agent_sid_on_msg(self, sid: str, msg_id: str, claude_sid: str):
        self.agent_msg_ids.append(msg_id)

    def set_stopped_at(self, sid: str, msg_id: str, value):
        pass

    def set_streaming(self, *args, **kwargs):
        pass

    def set_msg_completed(self, sid: str, msg_id: str, value):
        self.completed_msg_ids.append(msg_id)

    def set_msg_stopped(self, *args, **kwargs):
        pass

    def update_running_content(self, *args, **kwargs):
        pass

    def get(self, sid: str):
        return self.sess

    def set_msg_retrying_until(self, *args, **kwargs):
        pass

    def set_msg_transient_attempt(self, sid: str, msg_id: str, value: int):
        msg = next(
            (
                item for item in self.sess.get("messages", [])
                if isinstance(item, dict) and item.get("id") == msg_id
            ),
            None,
        )
        if msg is not None:
            msg["transient_attempt"] = value


def test_recovery_targets_descriptor_message_not_latest() -> None:
    print("T1 recovery mutates descriptor target, not latest assistant")
    sess = {
        "messages": [
            {"id": "target-msg", "role": "assistant", "events": []},
            {"id": "latest-msg", "role": "assistant", "events": []},
        ],
    }
    fake_sm = _FakeSessionManager(sess)
    replayed: list[str] = []

    original_sm = run_recovery.session_manager
    original_replay = run_recovery._replay_and_apply
    original_completion = run_recovery._apply_completion_state

    def _fake_replay(**kwargs):
        replayed.append(kwargs["msg_id"])

    def _fake_completion(persist_sid, msg_id, **_kwargs):
        fake_sm.set_msg_completed(persist_sid, msg_id, True)

    try:
        run_recovery.session_manager = fake_sm
        run_recovery._replay_and_apply = _fake_replay
        run_recovery._apply_completion_state = _fake_completion
        run_recovery._apply_integration_sync(
            persist_sid="sid",
            run_id="run-1",
            mode="native",
            claude_sid="provider-sid",
            sess=sess,
            alive=False,
            has_complete=True,
            cancelled=False,
            target_message_id="target-msg",
        )
    finally:
        run_recovery.session_manager = original_sm
        run_recovery._replay_and_apply = original_replay
        run_recovery._apply_completion_state = original_completion

    check("replayed target message", replayed == ["target-msg"])
    check("latest message untouched", "latest-msg" not in fake_sm.completed_msg_ids)


def test_recovery_missing_target_does_not_mutate_latest() -> None:
    print("T2 missing recovery target does not fall back to latest")
    sess = {
        "messages": [
            {"id": "latest-msg", "role": "assistant", "events": []},
        ],
    }
    fake_sm = _FakeSessionManager(sess)
    replayed: list[str] = []

    original_sm = run_recovery.session_manager
    original_replay = run_recovery._replay_and_apply

    def _fake_replay(**kwargs):
        replayed.append(kwargs["msg_id"])

    try:
        run_recovery.session_manager = fake_sm
        run_recovery._replay_and_apply = _fake_replay
        run_recovery._apply_integration_sync(
            persist_sid="sid",
            run_id="run-1",
            mode="native",
            claude_sid="provider-sid",
            sess=sess,
            alive=False,
            has_complete=True,
            cancelled=False,
            target_message_id=None,
        )
    finally:
        run_recovery.session_manager = original_sm
        run_recovery._replay_and_apply = original_replay

    check("no replay without target", replayed == [])


class _StubCoordinator:
    async def broadcast_session(self, *args, **kwargs):
        pass


def test_same_target_native_run_state_replaces_but_workers_stay_distinct() -> None:
    print("T3 run_state dedupes same native target but keeps worker runs")
    tm = TurnManager(_StubCoordinator())
    sid = "sid"
    tm.run_state_add(sid, run_id="native-1", kind="native", target_message_id="msg")
    tm.run_state_add(sid, run_id="native-2", kind="native", target_message_id="msg")
    tm.run_state_add(
        sid,
        run_id="worker-1",
        kind="worker",
        target_message_id="msg",
        delegation_id="d1",
    )
    tm.run_state_add(
        sid,
        run_id="worker-2",
        kind="worker",
        target_message_id="msg",
        delegation_id="d2",
    )
    run_ids = [r["run_id"] for r in tm._run_state[sid]]
    check("native same-target replaced", "native-1" not in run_ids and "native-2" in run_ids)
    check("worker same-target entries preserved", "worker-1" in run_ids and "worker-2" in run_ids)


def test_gemini_live_orphan_returns_live_descriptor_without_complete_json() -> None:
    print("T4 gemini live orphan remains live during recovery scan")
    provider = GeminiProvider({"id": "gemini-test", "kind": "gemini"})
    run_id = "gemini-live-run"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "backend_state.json", {
        "run_id": run_id,
        "app_session_id": "sid",
        "persist_to": "sid",
        "mode": "native",
        "runner_pid": os.getpid(),
        "started_at": "2026-01-01T00:00:00",
        "session_id": "gemini-session",
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "processed_line": 0,
        "cancelled": False,
        "provider_id": "gemini-test",
        "target_message_id": "msg",
    })

    recovered = provider.recover_in_flight()
    desc = recovered[0] if recovered else {}
    check("returned descriptor", len(recovered) == 1)
    check("descriptor is live", desc.get("alive") is True)
    check("descriptor has no complete", desc.get("has_complete_json") is False)
    check("complete.json not synthesized", not (run_dir / "complete.json").exists())


def test_openai_live_orphan_returns_live_descriptor_without_complete_json() -> None:
    print("T4b openai live orphan remains live during recovery scan")
    provider = OpenAIProvider({
        "id": "openai-test",
        "kind": "openai",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "test",
    })
    run_id = "openai-live-run"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "backend_state.json", {
        "run_id": run_id,
        "app_session_id": "sid",
        "persist_to": "sid",
        "mode": "native",
        "runner_pid": os.getpid(),
        "started_at": "2026-01-01T00:00:00",
        "session_id": "openai-session",
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "processed_line": 0,
        "cancelled": False,
        "provider_id": "openai-test",
        "provider_kind": "openai",
        "target_message_id": "msg",
    })

    recovered = provider.recover_in_flight(run_id_filter={run_id})
    desc = recovered[0] if recovered else {}
    check("openai returned descriptor", len(recovered) == 1)
    check("openai descriptor is live", desc.get("alive") is True)
    check("openai descriptor has no complete", desc.get("has_complete_json") is False)
    check("openai complete.json not synthesized", not (run_dir / "complete.json").exists())


def test_missing_target_finalizer_does_not_mark_reconciled() -> None:
    print("T5 missing-target live finalizer leaves run unreconciled")
    run_id = "missing-target-live"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "complete.json", {
        "success": True,
        "session_id": "provider-sid",
        "token_usage": None,
    })
    fake_sm = _FakeSessionManager({
        "messages": [{"id": "latest-msg", "role": "assistant", "events": []}],
    })

    class _Provider:
        def __init__(self) -> None:
            self._runs = {run_id: object()}

    class _TurnManager:
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.active_run_ids = {"sid": [run_id]}

        def run_state_remove(self, sid: str, rid: str) -> None:
            self.removed.append(rid)

        async def emit_run_state(self, sid: str) -> None:
            pass

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()

    original_sm = run_recovery.session_manager
    try:
        run_recovery.session_manager = fake_sm
        coordinator = _Coordinator()
        provider = _Provider()
        asyncio.run(run_recovery._finalize_when_done(
            coordinator,
            provider,
            {
                "run_id": run_id,
                "app_session_id": "sid",
                "persist_to": "sid",
                "pid": None,
                "mode": "native",
                "session_id": "provider-sid",
            },
            recovering_msg_id=None,
        ))
    finally:
        run_recovery.session_manager = original_sm

    check("run state removed after process ended", coordinator.turn_manager.removed == [run_id])
    check("provider run removed", run_id not in provider._runs)
    check("no reconciled marker written", not (run_dir / "reconciled.marker").exists())


def test_missing_target_completed_startup_does_not_mark_reconciled() -> None:
    print("T6 missing-target completed startup recovery stays unreconciled")
    run_id = "missing-target-complete"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    fake_sm = _FakeSessionManager({
        "messages": [{"id": "latest-msg", "role": "assistant", "events": []}],
    })

    class _Provider:
        _runs = {}

    class _TurnManager:
        active_run_ids = {}

        def run_state_add(self, *args, **kwargs):
            raise AssertionError("run_state_add should not run")

    class _Coordinator:
        turn_manager = _TurnManager()

    original_sm = run_recovery.session_manager
    try:
        run_recovery.session_manager = fake_sm
        asyncio.run(run_recovery._integrate_one(
            _Coordinator(),
            _Provider(),
            {
                "run_id": run_id,
                "app_session_id": "sid",
                "persist_to": "sid",
                "alive": False,
                "has_complete_json": True,
                "cancelled": False,
                "mode": "native",
                "session_id": "provider-sid",
            },
        ))
    finally:
        run_recovery.session_manager = original_sm

    check("no reconciled marker written", not (run_dir / "reconciled.marker").exists())


def test_retry_recovered_run_uses_passed_coordinator() -> None:
    print("T7 recovered retry registers via passed coordinator")
    run_id = "retry-needs-coordinator"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "input.json", {
        "prompt": "retry me",
        "source": "mssg",
        "cwd": "/tmp",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "token",
    })
    fake_sm = _FakeSessionManager({
        "agent_session_id": "provider-sid",
        "messages": [
            {"id": "msg-1", "role": "assistant", "events": [], "transient_attempt": 0},
        ],
    })

    class _Run:
        class _Popen:
            pid = None
        popen = _Popen()

    class _Provider:
        def __init__(self) -> None:
            self._runs = {}
            self.started: list[str] = []
            self.kwargs: dict = {}

        def start_run(self, *, run_id: str, **kwargs) -> None:
            self.started.append(run_id)
            self.kwargs = kwargs
            self._runs[run_id] = _Run()

    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}
            self.added: list[tuple[str, str]] = []
            self.emitted: list[str] = []

        def run_state_add(self, sid: str, *, run_id: str, **kwargs) -> None:
            self.added.append((sid, run_id))

        async def emit_run_state(self, sid: str) -> None:
            self.emitted.append(sid)

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()

    async def _sleep(_seconds: float) -> None:
        return None

    def _create_task(coro, *, name=None):
        coro.close()
        return None

    original_sm = run_recovery.session_manager
    original_sleep = run_recovery.asyncio.sleep
    original_create_task = run_recovery.asyncio.create_task
    try:
        run_recovery.session_manager = fake_sm
        run_recovery.asyncio.sleep = _sleep
        run_recovery.asyncio.create_task = _create_task
        coordinator = _Coordinator()
        provider = _Provider()
        asyncio.run(run_recovery._retry_recovered_run(
            coordinator=coordinator,
            provider=provider,
            desc={
                "run_id": run_id,
                "app_session_id": "sid",
                "persist_to": "sid",
                "mode": "native",
                "session_id": "provider-sid",
            },
            run_dir=run_dir,
            app_sid="sid",
            persist_sid="sid",
            msg_id="msg-1",
            recovering_msg_id="msg-1",
        ))
    finally:
        run_recovery.session_manager = original_sm
        run_recovery.asyncio.sleep = original_sleep
        run_recovery.asyncio.create_task = original_create_task

    new_run_id = provider.started[0] if provider.started else ""
    check("provider retried run", bool(new_run_id))
    check("active_run_ids updated", coordinator.turn_manager.active_run_ids == {"sid": [new_run_id]})
    check("run_state_add used coordinator", coordinator.turn_manager.added == [("sid", new_run_id)])
    check("run_state emitted", coordinator.turn_manager.emitted == ["sid"])
    check("retry preserves source", provider.kwargs.get("source") == "mssg")


def main() -> int:
    try:
        test_recovery_targets_descriptor_message_not_latest()
        test_recovery_missing_target_does_not_mutate_latest()
        test_same_target_native_run_state_replaces_but_workers_stay_distinct()
        test_gemini_live_orphan_returns_live_descriptor_without_complete_json()
        test_openai_live_orphan_returns_live_descriptor_without_complete_json()
        test_missing_target_finalizer_does_not_mark_reconciled()
        test_missing_target_completed_startup_does_not_mark_reconciled()
        test_retry_recovered_run_uses_passed_coordinator()
        print()
        if failures:
            print(f"FAILED: {len(failures)} check(s): {failures}")
            return 1
        print("ALL PASS")
        return 0
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"])


if __name__ == "__main__":
    sys.exit(main())
