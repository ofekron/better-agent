import contextlib
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_retarget_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_recovery  # noqa: E402
from execution_artifact_io import bind_execution_input  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from provider_agy import AgyProvider  # noqa: E402
from provider_execution_contract import provider_family_contract  # noqa: E402
from provider_family_execution_runtime import (  # noqa: E402
    family_capability_manifest_from_artifact,
    family_launch_from_artifact,
)
from provider_family_launch_attestation import (  # noqa: E402
    FamilyLaunchAttestation,
    capture_config_scope,
)
from provider_family_runtime_capabilities import (  # noqa: E402
    install_staged_family_runtime_capabilities,
    snapshot_family_runtime_capabilities,
    stage_family_runtime_capabilities,
)
from provider_launch_identity import capture_cli_launch  # noqa: E402
from provider_openai import OpenAIProvider  # noqa: E402
from provider_runner_launch import capture_runner_launch  # noqa: E402
from runs_dir import runs_root, atomic_write_json  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

failures: list[str] = []


def _write_execution_artifact(
    run_dir: Path,
    run_id: str,
    payload: dict,
    *,
    kind: str = "codex",
) -> None:
    prepared = prepare_execution(
        {
            "id": f"{kind}-provider",
            "kind": kind,
            "generation": "d5e1419b-d509-4c6c-a318-338961e75cf4",
            "revision": 2,
        },
        run_id=run_id,
        prompt=payload.get("prompt", ""),
        images=payload.get("images"),
        files=payload.get("files"),
        cwd=payload.get("cwd", "/tmp"),
        model=payload.get("model"),
        reasoning_effort=payload.get("reasoning_effort"),
        session_id=payload.get("session_id"),
        mode=payload.get("mode", "native"),
        app_session_id="sid",
        source=payload.get("source"),
        backend_url=payload.get("backend_url"),
        continuation_chain=payload.get("continuation_chain"),
        provider_run_config=payload.get("provider_run_config"),
        capability_contexts=payload.get("capability_contexts"),
        resolved_harness_run_config=payload.get("resolved_harness_run_config"),
        target_message_id=payload.get("target_message_id"),
        turn_run_id=payload.get("turn_run_id"),
        disabled_builtin_extensions=payload.get("disabled_builtin_extensions"),
        provisioned_tool_profile=payload.get("provisioned_tool_profile", ""),
    )
    atomic_write_json(run_dir / "execution.json", prepared.artifact.to_dict())
    atomic_write_json(
        run_dir / "input.json",
        bind_execution_input(prepared.artifact, {
            **prepared.artifact.template.arguments(),
            **payload,
        }),
    )


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

    def flush_pending_persists(self, *args, **kwargs):
        pass

    def set_completed_at(self, *args, **kwargs):
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

    def set_continuation_chain(self, sid: str, chain: list[str]):
        self.sess["continuation_chain"] = list(chain)


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


def test_session_events_live_orphan_returns_live_descriptor_without_complete_json() -> None:
    print("T4 session-events live orphan remains live during recovery scan")
    provider = AgyProvider({"id": "agy-test", "kind": "agy"})
    run_id = "agy-live-run"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "backend_state.json", {
        "run_id": run_id,
        "app_session_id": "sid",
        "persist_to": "sid",
        "mode": "native",
        "runner_pid": os.getpid(),
        "started_at": "2026-01-01T00:00:00",
        "session_id": "agy-session",
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "processed_line": 0,
        "cancelled": False,
        "provider_id": "agy-test",
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


def test_missing_target_finalizer_marks_reconciled() -> None:
    print("T5 missing-target live finalizer tombstones the run")
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

        def _cleanup_run(self, rid: str) -> None:
            self._runs.pop(rid, None)

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
    # Finalize succeeded; a version-stale desc with no native source can
    # never be re-digested, so the run is tombstoned instead of being
    # re-finalized on every startup.
    check("reconciled marker written", (run_dir / "reconciled.marker").exists())


def test_missing_target_completed_startup_marks_reconciled() -> None:
    print("T6 version-stale completed run is tombstoned, not re-ground")
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

    # Version-stale + native-source-missing is permanent — tombstoned so
    # startup recovery stops re-queueing it forever.
    check("reconciled marker written", (run_dir / "reconciled.marker").exists())


def test_retry_recovered_run_preserves_artifact_resume_sid() -> None:
    print("T7 recovered retry preserves artifact resume sid")
    run_id = "retry-needs-coordinator"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "prompt": "retry me",
        "session_id": "artifact-provider-sid",
        "source": "mssg",
        "cwd": "/tmp",
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "token",
        "resolved_harness_run_config": {
            "profile_id": "recovery-profile",
            "extra_mcp_servers": ["recovery-server"],
        },
        "disabled_builtin_extensions": ["recovery.disabled"],
        "provisioned_tool_profile": "",
    }
    _write_execution_artifact(run_dir, run_id, input_payload, kind="remote")
    fake_sm = _FakeSessionManager({
        "messages": [
            {"id": "msg-1", "role": "assistant", "events": [], "transient_attempt": 0},
        ],
    })

    class _Run:
        class _Popen:
            pid = None
        popen = _Popen()

    class _Provider:
        KIND = "remote"

        def __init__(self) -> None:
            self._runs = {}
            self.started: list[str] = []
            self.kwargs: dict = {}

        def start_run(self, *, execution, **_kwargs) -> None:
            kwargs = execution.start_arguments()
            run_id = kwargs["run_id"]
            self.started.append(run_id)
            self.kwargs = kwargs
            self._runs[run_id] = _Run()
            return True

    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}
            self.cancel_events = {}
            self.added: list[tuple[str, str]] = []
            self.submitted: list[tuple[str, str, str]] = []
            self.emitted: list[str] = []

        def run_state_add(self, sid: str, *, run_id: str, **kwargs) -> None:
            self.added.append((sid, run_id))

        def run_state_mark_provider_submitted(
            self, sid: str, run_id: str, provider_kind: str,
        ) -> None:
            self.submitted.append((sid, run_id, provider_kind))

        def run_state_mark_retrying(self, _sid: str, _run_id: str) -> None:
            pass

        def run_state_set_pid(self, _sid: str, _run_id: str, _pid) -> None:
            pass

        async def emit_run_state(self, sid: str) -> None:
            self.emitted.append(sid)

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()
            self._session_cancelled = {}
            self.internal_token = "test-token"

    async def _sleep(_seconds: float) -> None:
        return None

    async def _wait_for(awaitable, *, timeout: float):
        awaitable.close()
        raise asyncio.TimeoutError()

    def _create_task(coro, *, name=None):
        coro.close()
        return None

    original_sm = run_recovery.session_manager
    original_sleep = run_recovery.asyncio.sleep
    original_wait_for = run_recovery.asyncio.wait_for
    original_create_task = run_recovery.asyncio.create_task
    try:
        run_recovery.session_manager = fake_sm
        run_recovery.asyncio.sleep = _sleep
        run_recovery.asyncio.wait_for = _wait_for
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
                "session_id": None,
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
        run_recovery.asyncio.wait_for = original_wait_for
        run_recovery.asyncio.create_task = original_create_task

    new_run_id = provider.started[0] if provider.started else ""
    check("provider retried run", bool(new_run_id))
    check("active_run_ids updated", coordinator.turn_manager.active_run_ids == {"sid": [new_run_id]})
    check("run_state_add used coordinator", coordinator.turn_manager.added == [("sid", new_run_id)])
    check(
        "provider startup monitoring registered",
        coordinator.turn_manager.submitted == [("sid", new_run_id, "remote")],
    )
    check("retry-pending and submitted state emitted", coordinator.turn_manager.emitted == ["sid", "sid"])
    check("retry preserves source", provider.kwargs.get("source") == "mssg")
    assert provider.kwargs.get("session_id") == "artifact-provider-sid"
    check(
        "retry preserves resolved harness snapshot",
        provider.kwargs.get("resolved_harness_run_config", {}).get("profile_id")
        == "recovery-profile",
    )
    check(
        "retry preserves disabled extensions",
        provider.kwargs.get("disabled_builtin_extensions") == ["recovery.disabled"],
    )


def test_unbound_recovered_input_is_terminal() -> None:
    print("T7legacy unbound recovered input is terminal")
    run_id = "retry-unbound-legacy"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"prompt": "legacy retry", "cwd": "/tmp"}
    _write_execution_artifact(run_dir, run_id, payload)
    bound = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    bound.pop("execution_fingerprint")
    atomic_write_json(run_dir / "input.json", bound)

    asyncio.run(run_recovery._retry_recovered_run(
        coordinator=None,
        provider=None,
        desc={"run_id": run_id},
        run_dir=run_dir,
        app_sid="sid",
        persist_sid="sid",
        msg_id="msg-1",
        recovering_msg_id="msg-1",
    ))

    check(
        "legacy run authority is terminally classified",
        (run_dir / "reconciled.marker").exists(),
    )


def test_family_retry_clones_exact_runtime_capabilities() -> None:
    print("T7a family retry clones exact runtime capabilities")
    run_id = "family-retry-capabilities"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "prompt": "retry with the prepared scheduler",
        "cwd": "/tmp",
        "model": "agy-frozen",
    }
    capabilities = snapshot_family_runtime_capabilities(
        family="agy",
        skill_sources={},
        agent_sources={},
        resolved_plan={
            "harness": {"instructions": ["frozen scheduler harness"]},
            "tools": ["scheduler_run"],
            "mcp_servers": [{
                "name": "scheduler",
                "transport": "stdio",
                "config": {
                    "command": "scheduler",
                    "args": ["serve"],
                },
                "tool_names": ["scheduler_run"],
                "prewarm": {
                    "eligible": False,
                    "readiness_required": False,
                },
            }],
        },
        extension_state={"scheduler": {"enabled": True}},
        installation_decisions={"profile": "frozen"},
    )
    provider_record = {
        "id": "agy-provider",
        "kind": "agy",
        "generation": "d5e1419b-d509-4c6c-a318-338961e75cf4",
        "revision": 2,
    }
    downstream = run_dir / "agy"
    downstream.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    downstream.chmod(0o700)
    config_root = run_dir / "config"
    config_root.mkdir()
    launch = FamilyLaunchAttestation.capture(
        family="agy",
        runner=capture_runner_launch(
            run_dir=run_dir,
            executable_path=sys.executable,
            runner_entry=Path(__file__).parent.parent / "runner_agy.py",
            runner_kind="agy",
            runner_module="runner_agy",
            frozen=False,
        ),
        downstream=capture_cli_launch(
            logical_command="agy",
            launcher_path=downstream,
        ),
        config=capture_config_scope(root_path=config_root),
    )
    prepared = prepare_execution(
        provider_record,
        run_id=run_id,
        prompt=input_payload["prompt"],
        cwd=input_payload["cwd"],
        model=input_payload["model"],
        reasoning_effort=None,
        session_id="agy-provider-sid",
        mode="native",
        app_session_id="sid",
        runtime_policy={
            "runner_input": {
                **input_payload,
                "run_id": run_id,
                "session_id": "agy-provider-sid",
                "mode": "native",
                "app_session_id": "sid",
                "provider_id": provider_record["id"],
            },
        },
        provider_contract=provider_family_contract(
            provider_record,
            payload={
                **launch.to_payload(),
                "runtime_capabilities": capabilities.manifest,
            },
        ),
    )
    atomic_write_json(
        run_dir / "input.json",
        bind_execution_input(
            prepared.artifact,
            prepared.artifact.runtime_policy["runner_input"],
        ),
    )
    atomic_write_json(run_dir / "execution.json", prepared.artifact.to_dict())
    stage_family_runtime_capabilities(run_id, capabilities)
    install_staged_family_runtime_capabilities(
        run_dir,
        run_id=run_id,
        manifest=capabilities.manifest,
    )
    source_payload = (
        run_dir / capabilities.manifest["path"]
    ).read_bytes()
    fake_sm = _FakeSessionManager({
        "agent_session_id": "agy-provider-sid",
        "messages": [
            {"id": "msg-1", "role": "assistant", "events": []},
        ],
    })

    class _Run:
        class _Popen:
            pid = None
        popen = _Popen()

    class _Provider:
        KIND = "agy"

        def __init__(self) -> None:
            self._runs = {}
            self.target_payload = b""
            self.target_contract: dict = {}
            self.target_runner_input: dict = {}
            self.target_launch = None
            self.target_capability_manifest: dict = {}

        def start_run(self, *, execution, **_kwargs) -> bool:
            target_run_id = execution.start_arguments()["run_id"]
            target_run_dir = runs_root() / target_run_id
            target_run_dir.mkdir(parents=True, exist_ok=False)
            install_staged_family_runtime_capabilities(
                target_run_dir,
                run_id=target_run_id,
                manifest=family_capability_manifest_from_artifact(
                    execution.artifact,
                ),
            )
            self.target_payload = (
                target_run_dir / capabilities.manifest["path"]
            ).read_bytes()
            self.target_contract = execution.artifact.provider_contract or {}
            self.target_runner_input = execution.artifact.runtime_policy[
                "runner_input"
            ]
            self.target_launch = family_launch_from_artifact(
                execution.artifact,
            )
            self.target_capability_manifest = (
                family_capability_manifest_from_artifact(
                    execution.artifact,
                )
            )
            self._runs[target_run_id] = _Run()
            return True

    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}
            self.cancel_events = {}

        def run_state_add(self, *_args, **_kwargs) -> None:
            pass

        def run_state_mark_retrying(self, *_args, **_kwargs) -> None:
            pass

        def run_state_mark_provider_submitted(self, *_args, **_kwargs) -> None:
            pass

        def run_state_set_pid(self, *_args, **_kwargs) -> None:
            pass

        async def emit_run_state(self, _sid: str) -> None:
            pass

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()
            self._session_cancelled = {}
            self.internal_token = "retry-token"

    async def _skip_backoff(awaitable, *, timeout: float):
        awaitable.close()
        raise asyncio.TimeoutError()

    def _create_task(coro, *, name=None):
        coro.close()
        return None

    original_sm = run_recovery.session_manager
    original_wait_for = run_recovery.asyncio.wait_for
    original_create_task = run_recovery.asyncio.create_task
    try:
        run_recovery.session_manager = fake_sm
        run_recovery.asyncio.wait_for = _skip_backoff
        run_recovery.asyncio.create_task = _create_task
        provider = _Provider()
        asyncio.run(run_recovery._retry_recovered_run(
            coordinator=_Coordinator(),
            provider=provider,
            desc={
                "run_id": run_id,
                "app_session_id": "sid",
                "persist_to": "sid",
                "mode": "native",
                "session_id": "agy-provider-sid",
            },
            run_dir=run_dir,
            app_sid="sid",
            persist_sid="sid",
            msg_id="msg-1",
            recovering_msg_id="msg-1",
        ))
    finally:
        run_recovery.session_manager = original_sm
        run_recovery.asyncio.wait_for = original_wait_for
        run_recovery.asyncio.create_task = original_create_task

    check("family retry preserves exact payload bytes", provider.target_payload == source_payload)
    check(
        "family retry preserves exact capability authority",
        provider.target_capability_manifest
        == family_capability_manifest_from_artifact(prepared.artifact)
        == capabilities.manifest,
    )
    check(
        "family retry updates frozen runner identity",
        provider.target_runner_input.get("run_id") != run_id,
    )
    source_launch = family_launch_from_artifact(prepared.artifact)
    target_run_id = provider.target_runner_input["run_id"]
    check(
        "family retry rebuilds runner launch for target directory",
        provider.target_launch is not None
        and provider.target_launch.runner.launch.argv[3]
        == str(runs_root() / target_run_id),
    )
    check(
        "family retry preserves downstream authority",
        provider.target_launch is not None
        and provider.target_launch.downstream == source_launch.downstream,
    )
    check(
        "family retry replaces stale provider contract",
        provider.target_contract != prepared.artifact.provider_contract,
    )


def test_recovered_retry_cancelled_during_backoff_does_not_spawn() -> None:
    print("T7b recovered retry cancellation prevents spawn")
    run_id = "retry-cancelled-before-spawn"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "prompt": "do not retry me",
        "cwd": "/tmp",
    }
    _write_execution_artifact(run_dir, run_id, input_payload)
    fake_sm = _FakeSessionManager({
        "messages": [
            {"id": "msg-1", "role": "assistant", "events": [], "transient_attempt": 0},
        ],
    })

    class _Provider:
        KIND = "codex"
        _runs = {}

        def __init__(self) -> None:
            self.started = 0

        def start_run(self, **_kwargs) -> None:
            self.started += 1
            return True

    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}
            self.cancel_events = {}
            self.removed = []

        def run_state_add(self, *_args, **_kwargs) -> None:
            pass

        def run_state_mark_retrying(self, *_args, **_kwargs) -> None:
            pass

        def run_state_remove(self, sid: str, run_id: str) -> None:
            self.removed.append((sid, run_id))

        async def emit_run_state(self, _sid: str) -> None:
            pass

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()
            self._session_cancelled = {}

    coordinator = _Coordinator()

    async def _cancel_wait(awaitable, *, timeout: float):
        awaitable.close()
        event = coordinator.turn_manager.cancel_events["sid"]
        coordinator._session_cancelled["sid"] = True
        event.set()

    original_sm = run_recovery.session_manager
    original_wait_for = run_recovery.asyncio.wait_for
    try:
        run_recovery.session_manager = fake_sm
        run_recovery.asyncio.wait_for = _cancel_wait
        provider = _Provider()
        asyncio.run(run_recovery._retry_recovered_run(
            coordinator=coordinator,
            provider=provider,
            desc={"run_id": run_id, "mode": "native"},
            run_dir=run_dir,
            app_sid="sid",
            persist_sid="sid",
            msg_id="msg-1",
            recovering_msg_id="msg-1",
        ))
    finally:
        run_recovery.session_manager = original_sm
        run_recovery.asyncio.wait_for = original_wait_for

    check("provider was not spawned", provider.started == 0)
    check("retry registration removed", coordinator.turn_manager.active_run_ids == {})
    check("cancel event removed", coordinator.turn_manager.cancel_events == {})
    check("run state removed", len(coordinator.turn_manager.removed) == 1)


def test_recovered_retry_spawn_boundaries_cleanup() -> None:
    print("T7c recovered retry spawn races clean up")
    run_id = "retry-spawn-boundaries"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "prompt": "retry safely",
        "cwd": "/tmp",
    }
    _write_execution_artifact(run_dir, run_id, input_payload)

    class _Run:
        class _Popen:
            pid = None
        popen = _Popen()

    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}
            self.cancel_events = {}
            self.removed = []

        def run_state_add(self, *_args, **_kwargs) -> None:
            pass

        def run_state_mark_retrying(self, *_args, **_kwargs) -> None:
            pass

        def run_state_remove(self, sid: str, retry_run_id: str) -> None:
            self.removed.append((sid, retry_run_id))

        def run_state_set_pid(self, *_args, **_kwargs) -> None:
            pass

        def run_state_mark_provider_submitted(self, *_args, **_kwargs) -> None:
            pass

        async def emit_run_state(self, _sid: str) -> None:
            pass

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()
            self._session_cancelled = {}
            self.cancelled_runs = []
            self.internal_token = "test-token"

        def _cancel_turn_fanout(self, retry_run_id: str) -> None:
            self.cancelled_runs.append(retry_run_id)

    async def _skip_backoff(awaitable, *, timeout: float):
        awaitable.close()
        raise asyncio.TimeoutError()

    def _create_task(coro, *, name=None):
        coro.close()
        return None

    async def _run(provider, coordinator) -> None:
        await run_recovery._retry_recovered_run(
            coordinator=coordinator,
            provider=provider,
            desc={"run_id": run_id, "mode": "native"},
            run_dir=run_dir,
            app_sid="sid",
            persist_sid="sid",
            msg_id="msg-1",
            recovering_msg_id="msg-1",
        )

    original_sm = run_recovery.session_manager
    original_wait_for = run_recovery.asyncio.wait_for
    original_create_task = run_recovery.asyncio.create_task
    import codex_execution_runtime

    original_retry_codex = codex_execution_runtime.retry_codex_execution
    codex_execution_runtime.retry_codex_execution = (
        lambda execution, **overrides: execution.retry(**overrides)
    )
    try:
        run_recovery.asyncio.wait_for = _skip_backoff
        run_recovery.asyncio.create_task = _create_task

        race_coordinator = _Coordinator()

        class _RaceProvider:
            KIND = "codex"

            def __init__(self) -> None:
                self._runs = {}
                self.started = 0

            def start_run(self, *, execution, **_kwargs) -> None:
                run_id = execution.start_arguments()["run_id"]
                self.started += 1
                self._runs[run_id] = _Run()
                race_coordinator._session_cancelled["sid"] = True
                race_coordinator.turn_manager.cancel_events["sid"].set()
                return True

        run_recovery.session_manager = _FakeSessionManager({
            "messages": [{"id": "msg-1", "role": "assistant", "events": []}],
        })
        race_provider = _RaceProvider()
        asyncio.run(_run(race_provider, race_coordinator))
        check("spawn race starts exactly once", race_provider.started == 1)
        check("spawn race fans out cancellation", len(race_coordinator.cancelled_runs) == 1)

        failing_coordinator = _Coordinator()

        class _FailingProvider:
            KIND = "codex"
            _runs = {}

            def start_run(self, **_kwargs) -> None:
                raise RuntimeError("spawn failed")

        run_recovery.session_manager = _FakeSessionManager({
            "messages": [{"id": "msg-1", "role": "assistant", "events": []}],
        })
        try:
            asyncio.run(_run(_FailingProvider(), failing_coordinator))
        except RuntimeError as error:
            check("spawn exception propagated", str(error) == "spawn failed")
        else:
            check("spawn exception propagated", False)
        check("spawn exception clears active id", failing_coordinator.turn_manager.active_run_ids == {})
        check("spawn exception clears cancel event", failing_coordinator.turn_manager.cancel_events == {})
        check("spawn exception clears run state", len(failing_coordinator.turn_manager.removed) == 1)
    finally:
        codex_execution_runtime.retry_codex_execution = original_retry_codex
        run_recovery.session_manager = original_sm
        run_recovery.asyncio.wait_for = original_wait_for
        run_recovery.asyncio.create_task = original_create_task


def test_recovered_capability_change_starts_fresh_continuation() -> None:
    print("T8 recovered capability change starts fresh continuation")
    run_id = "recovered-capability-change"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_payload = {
        "prompt": "STALE WRAPPED RECOVERY PROMPT",
        "cwd": "/tmp",
        "model": "gpt",
        "continuation_chain": [],
    }
    _write_execution_artifact(run_dir, run_id, input_payload)
    fake_sm = _FakeSessionManager({
        "agent_session_id": "newer-session-global-sid",
        "messages": [
            {
                "id": "prior-user",
                "role": "user",
                "content": "Why does Agent Board not own MCP services?",
                "events": [],
            },
            {
                "id": "prior-assistant",
                "role": "assistant",
                "content": (
                    "Agent Board owns durable board state; "
                    "services have separate owners."
                ),
                "completed_at": "2026-07-25T12:00:00Z",
                "events": [],
            },
            {
                "id": "current-user",
                "role": "user",
                "content": "why",
                "cli_prompt": "STALE CLI PROMPT",
                "events": [],
            },
            {"id": "msg-1", "role": "assistant", "content": "", "events": []},
        ],
    })

    class _Run:
        class _Popen:
            pid = None
        popen = _Popen()

    class _Provider:
        KIND = "codex"

        def __init__(self) -> None:
            self._runs = {}
            self.kwargs: dict = {}
            self.prepared = 0
            self.runtime_policy: dict = {}

        def prepare_run(self, **kwargs):
            self.prepared += 1
            return prepare_execution(
                {
                    "id": "codex-provider",
                    "kind": "codex",
                    "generation": "d5e1419b-d509-4c6c-a318-338961e75cf4",
                    "revision": 2,
                },
                runtime_policy={"fresh_capabilities": True},
                **kwargs,
            )

        def start_run(self, *, execution, **_kwargs) -> None:
            kwargs = execution.start_arguments()
            run_id = kwargs["run_id"]
            self.kwargs = kwargs
            self.runtime_policy = execution.artifact.runtime_policy
            self._runs[run_id] = _Run()
            return True

    class _TurnManager:
        def __init__(self) -> None:
            self.active_run_ids = {}

        def run_state_add(self, *_args, **_kwargs) -> None:
            pass

        def run_state_mark_provider_submitted(self, *_args, **_kwargs) -> None:
            pass

        async def emit_run_state(self, _sid: str) -> None:
            pass

    class _Coordinator:
        def __init__(self) -> None:
            self.turn_manager = _TurnManager()
            self.internal_token = "test-token"

    def _create_task(coro, *, name=None):
        coro.close()
        return None

    original_sm = run_recovery.session_manager
    original_create_task = run_recovery.asyncio.create_task
    try:
        run_recovery.session_manager = fake_sm
        run_recovery.asyncio.create_task = _create_task
        provider = _Provider()
        asyncio.run(run_recovery._retry_recovered_run(
            coordinator=_Coordinator(),
            provider=provider,
            desc={
                "run_id": run_id,
                "app_session_id": "sid",
                "persist_to": "sid",
                "mode": "native",
                "session_id": "old-provider-sid",
            },
            run_dir=run_dir,
            app_sid="sid",
            persist_sid="sid",
            msg_id="msg-1",
            recovering_msg_id="msg-1",
            fresh_continuation_reason="provider_capabilities_changed",
        ))
    finally:
        run_recovery.session_manager = original_sm
        run_recovery.asyncio.create_task = original_create_task

    check("fresh recovery omits resume sid", provider.kwargs.get("session_id") is None)
    check("fresh recovery prepares new authority", provider.prepared == 1)
    check(
        "fresh recovery uses new capability authority",
        provider.runtime_policy.get("fresh_capabilities") is True,
    )
    check(
        "fresh recovery persists old sid in chain",
        fake_sm.sess.get("continuation_chain") == ["old-provider-sid"],
    )
    check(
        "fresh recovery wraps original prompt",
        "Available provider capabilities changed" in provider.kwargs.get("prompt", ""),
    )
    check(
        "fresh recovery includes authoritative prior exchange",
        "Agent Board owns durable board state; services have separate owners."
        in provider.kwargs.get("prompt", ""),
    )
    check(
        "fresh recovery ends with persisted current user",
        provider.kwargs.get("prompt", "").endswith("why"),
    )
    check(
        "fresh recovery excludes stale runner prompt",
        "STALE WRAPPED RECOVERY PROMPT" not in provider.kwargs.get("prompt", ""),
    )
    check(
        "fresh recovery excludes transformed CLI prompt",
        "STALE CLI PROMPT" not in provider.kwargs.get("prompt", ""),
    )
    check(
        "fresh recovery forwards continuation chain",
        provider.kwargs.get("continuation_chain") == ["old-provider-sid"],
    )


def test_bounded_capability_recovery_targets_descriptor_message() -> None:
    print("T9 bounded capability recovery targets descriptor message")
    sess = {
        "messages": [
            {"id": "target-msg", "role": "assistant", "events": []},
            {"id": "latest-msg", "role": "assistant", "events": []},
        ],
    }
    target = run_recovery._recovery_target_assistant(sess, "target-msg")
    latest = run_recovery._last_assistant(sess)
    check("descriptor target resolved", target and target.get("id") == "target-msg")
    check("descriptor target remains distinct from latest", target is not latest)


def main() -> int:
    try:
        test_recovery_targets_descriptor_message_not_latest()
        test_recovery_missing_target_does_not_mutate_latest()
        test_same_target_native_run_state_replaces_but_workers_stay_distinct()
        test_session_events_live_orphan_returns_live_descriptor_without_complete_json()
        test_openai_live_orphan_returns_live_descriptor_without_complete_json()
        test_missing_target_finalizer_marks_reconciled()
        test_missing_target_completed_startup_marks_reconciled()
        test_retry_recovered_run_preserves_artifact_resume_sid()
        test_unbound_recovered_input_is_terminal()
        test_family_retry_clones_exact_runtime_capabilities()
        test_recovered_retry_cancelled_during_backoff_does_not_spawn()
        test_recovered_retry_spawn_boundaries_cleanup()
        test_recovered_capability_change_starts_fresh_continuation()
        test_bounded_capability_recovery_targets_descriptor_message()
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
