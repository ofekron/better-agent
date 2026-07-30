from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-task-runner-collapse-")

import task_runner
import config_store
from orchestrator import Coordinator
from scheduler import Scheduler
from session_manager import manager as session_manager
from stores import task_store, task_trigger_store


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _fake_coordinator(monkeypatch, *, stub_provisioning: bool = True):
    coordinator = Coordinator()
    submitted: list[dict] = []

    async def fake_submit_prompt_async(sid: str, params: dict, **_kwargs) -> str:
        # Mirror the real Coordinator.submit_prompt's item-id assignment,
        # since callers (task_runner) don't pre-stamp `_queued_id`.
        item_id = params.get("_queued_id") or uuid.uuid4().hex
        params["_queued_id"] = item_id
        q = coordinator._prompt_queues.setdefault(sid, asyncio.Queue())
        q.put_nowait(dict(params))
        coordinator._queued_ids.setdefault(sid, []).append(item_id)
        submitted.append(params)
        return item_id

    monkeypatch.setattr(coordinator, "submit_prompt_async", fake_submit_prompt_async)
    if stub_provisioning:
        import provisioning

        monkeypatch.setattr(provisioning, "resolve_config", lambda spec: spec.build_config())
        bases: dict[str, str] = {}

        async def ensure_warm_base(spec, cfg):
            if spec.key in bases:
                return bases[spec.key]
            base = session_manager.create(
                name=spec.name,
                model=cfg.model,
                provider_id=None,
                cwd=cfg.cwd,
                orchestration_mode=spec.orchestration_mode,
                source="internal",
                user_initiated=False,
                storage_scope=spec.storage_scope,
            )
            session_manager.set_agent_sid(base["id"], "native", f"base-{base['id']}")
            bases[spec.key] = base["id"]
            return base["id"]

        monkeypatch.setattr(provisioning, "ensure_warm_base", ensure_warm_base)
    return coordinator, submitted


def _turn_end_receipt(task: dict, event_key: str) -> str:
    task_trigger_store.register_for_task(task)
    created = task_trigger_store.enqueue_turn_end(
        event_type="lifecycle.turn_complete",
        event_key=event_key,
        root_id="source-root",
        session_id="source-session",
        reason="success",
        timestamp=datetime.now().isoformat(),
        provider_kind="codex",
        cwd=task["cwd"],
        node_id=task.get("node_id") or "primary",
    )
    assert created >= 1
    return next(
        item["id"]
        for item in task_trigger_store.due()
        if item.get("kind") == "turn_end_once" and item.get("task_id") == task["id"]
    )


def test_singleton_routine_fire_collapses_into_still_queued_prior_fire(monkeypatch):
    """A singleton routine that fires again before its previous fire has been
    dequeued (e.g. a slow RCA run outliving the scheduler interval) must
    collapse into the still-queued item instead of piling up a backlog."""
    task = task_store.create(
        cwd="/repo",
        name="monitor-logs",
        prompt="scan logs for errors",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)

    first = asyncio.run(task_runner.launch_task(
        task["id"], coordinator=coordinator, source="trigger",
    ))
    second = asyncio.run(task_runner.launch_task(
        task["id"], coordinator=coordinator, source="trigger",
    ))

    assert first["session_id"] == second["session_id"]
    assert second["queue_item_id"] == first["queue_item_id"]
    # Only one prompt ever reached submit_prompt_async's queue put — the
    # second fire collapsed instead of appending a second queued item.
    assert len(submitted) == 1
    q = coordinator._prompt_queues[first["session_id"]]
    assert q.qsize() == 1
    pending = q.get_nowait()
    assert pending["_queued_id"] == first["queue_item_id"]
    assert pending["collapse_key"] == f"routine:{task['id']}"
    assert uuid.UUID(pending["lifecycle_msg_id"])
    assert submitted[0]["lifecycle_msg_id"] == pending["lifecycle_msg_id"]


def test_singleton_routine_fire_after_prior_dequeued_does_not_collapse(monkeypatch):
    """Once the previous fire has actually been dequeued (run started), a new
    fire must enqueue normally rather than silently vanish."""
    task = task_store.create(
        cwd="/repo",
        name="monitor-logs-2",
        prompt="scan logs for errors",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)

    first = asyncio.run(task_runner.launch_task(
        task["id"], coordinator=coordinator, source="trigger",
    ))
    # Simulate the processor having dequeued the first fire already.
    coordinator._prompt_queues[first["session_id"]].get_nowait()

    second = asyncio.run(task_runner.launch_task(
        task["id"], coordinator=coordinator, source="trigger",
    ))

    assert second["queue_item_id"] != first["queue_item_id"]
    assert len(submitted) == 2
    assert uuid.UUID(submitted[0]["lifecycle_msg_id"])
    assert uuid.UUID(submitted[1]["lifecycle_msg_id"])
    assert submitted[0]["lifecycle_msg_id"] != submitted[1]["lifecycle_msg_id"]
    q = coordinator._prompt_queues[first["session_id"]]
    assert q.qsize() == 1


def test_concurrent_singleton_first_launch_creates_one_provisioned_fork(monkeypatch):
    task = task_store.create(
        cwd="/repo",
        name="monitor-concurrently",
        prompt="scan logs",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)

    async def launch_both():
        return await asyncio.gather(
            task_runner.launch_task(task["id"], coordinator=coordinator, source="trigger"),
            task_runner.launch_task(task["id"], coordinator=coordinator, source="trigger"),
        )

    first, second = asyncio.run(launch_both())
    assert first["session_id"] == second["session_id"]
    assert len(submitted) == 1
    assert task_store.get(task["id"])["singleton_session_id"] == first["session_id"]


def test_turn_end_receipt_replay_is_idempotent(monkeypatch):
    trigger = {
        "kind": "turn_end",
        "config": {
            "outcomes": ["complete"],
            "reasons": ["success"],
            "provider_kind": "codex",
        },
    }
    task = task_store.create(
        cwd="/repo",
        name="codex-ingestion-audit",
        prompt="audit the completed turn",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
        trigger=trigger,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)
    receipt_id = _turn_end_receipt(task, "receipt-1")
    kwargs = {
        "coordinator": coordinator,
        "source": "turn_end_trigger",
        "client_id": "routine-event:receipt-1",
        "event_receipt_id": receipt_id,
    }

    first = asyncio.run(task_runner.launch_task(task["id"], **kwargs))
    second = asyncio.run(task_runner.launch_task(task["id"], **kwargs))

    assert first["session_id"] == second["session_id"]
    assert first["queue_item_id"] == second["queue_item_id"]
    assert len(submitted) == 1
    stored = task_store.get(task["id"])
    assert stored is not None
    assert stored["run_count"] == 1
    assert stored["recent_runs"][0]["event_admission_state"] == "queued"
    assert stored["recent_runs"][0]["lifecycle_msg_id"] == submitted[0]["lifecycle_msg_id"]


def test_turn_end_crash_before_confirmation_retries_same_admission(monkeypatch):
    trigger = {
        "kind": "turn_end",
        "config": {"provider_kind": "codex", "outcomes": ["complete"]},
    }
    task = task_store.create(
        cwd="/repo",
        name="codex-ingestion-audit-crash",
        prompt="audit the completed turn",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
        trigger=trigger,
    )
    coordinator, first_submitted = _fake_coordinator(monkeypatch)
    receipt_id = _turn_end_receipt(task, "receipt-crash")
    real_confirm = task_store.confirm_event_run
    confirmations = 0

    def _drop_first_confirmation(*args, **kwargs):
        nonlocal confirmations
        confirmations += 1
        if confirmations == 1:
            return None
        return real_confirm(*args, **kwargs)

    monkeypatch.setattr(task_store, "confirm_event_run", _drop_first_confirmation)
    kwargs = {
        "coordinator": coordinator,
        "source": "turn_end_trigger",
        "client_id": "routine-event:receipt-crash",
        "event_receipt_id": receipt_id,
    }

    first = asyncio.run(task_runner.launch_task(task["id"], **kwargs))
    first_lifecycle_msg_id = first_submitted[0]["lifecycle_msg_id"]

    coordinator, retry_submitted = _fake_coordinator(
        monkeypatch, stub_provisioning=False,
    )
    kwargs["coordinator"] = coordinator
    second = asyncio.run(task_runner.launch_task(task["id"], **kwargs))

    assert first["queue_item_id"] == second["queue_item_id"]
    assert len(first_submitted) == len(retry_submitted) == 1
    assert retry_submitted[0]["lifecycle_msg_id"] == first_lifecycle_msg_id
    assert uuid.UUID(first_lifecycle_msg_id)
    stored = task_store.get(task["id"])
    assert stored is not None
    assert stored["run_count"] == 1
    assert stored["recent_runs"][0]["event_admission_state"] == "queued"
    assert stored["recent_runs"][0]["lifecycle_msg_id"] == first_lifecycle_msg_id


def test_confirmed_turn_end_prompt_survives_queue_loss(monkeypatch):
    trigger = {
        "kind": "turn_end",
        "config": {"provider_kind": "codex", "outcomes": ["complete"]},
    }
    task = task_store.create(
        cwd="/repo",
        name="confirmed-queue-recovery",
        prompt="recover the confirmed prompt",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
        trigger=trigger,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)
    receipt_id = _turn_end_receipt(task, "receipt-confirmed")

    launched = asyncio.run(task_runner.launch_task(
        task["id"],
        coordinator=coordinator,
        source="turn_end_trigger",
        client_id=f"routine-event:{receipt_id}",
        event_receipt_id=receipt_id,
    ))
    task_trigger_store.mark_fired(receipt_id)

    durable = session_manager.get(launched["session_id"])["queued_prompts"]
    assert len(durable) == 1
    assert durable[0]["lifecycle_msg_id"] == submitted[0]["lifecycle_msg_id"]

    restarted = Coordinator()
    restarted._queue_persisted_prompts_for_promotion(launched["session_id"])
    recovered_queue = restarted._prompt_queues[launched["session_id"]]
    assert recovered_queue.qsize() == 1
    recovered = recovered_queue.get_nowait()
    assert recovered["_queued_id"] == durable[0]["id"]
    assert recovered["lifecycle_msg_id"] == durable[0]["lifecycle_msg_id"]
    assert not any(item.get("id") == receipt_id for item in task_trigger_store.due())


def test_legacy_confirmed_receipt_without_identity_is_retired(monkeypatch):
    with task_trigger_store._lock:
        task_trigger_store._write(task_trigger_store._empty())
    trigger = {
        "kind": "turn_end",
        "config": {"provider_kind": "codex", "outcomes": ["complete"]},
    }
    task = task_store.create(
        cwd="/repo",
        name="legacy-confirmed-receipt",
        prompt="must fail closed without retrying forever",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
        trigger=trigger,
    )
    receipt_id = _turn_end_receipt(task, "receipt-legacy-confirmed")
    status, _ = task_trigger_store.claim_event_run(
        receipt_id,
        "legacy-session",
        lifecycle_msg_id=str(uuid.uuid4()),
        expected_task_updated_at=str(task["updated_at"]),
    )
    assert status == "admitted"
    task_store.confirm_event_run(
        task["id"], receipt_id, "legacy-queue-item",
    )
    with task_store._lock:
        data = task_store._read()
        stored_task = next(item for item in data["tasks"] if item["id"] == task["id"])
        stored_run = next(
            item for item in stored_task["recent_runs"]
            if item.get("event_receipt_id") == receipt_id
        )
        stored_run.pop("lifecycle_msg_id")
        task_store._write(data)

    coordinator, submitted = _fake_coordinator(monkeypatch)
    launched = asyncio.run(Scheduler(coordinator).fire_task_triggers())

    assert launched == 0
    assert not any(
        item.get("client_id") == f"routine-event:{receipt_id}"
        for item in submitted
    )
    assert not any(item.get("id") == receipt_id for item in task_trigger_store.due())


def test_turn_end_update_race_rejected_at_atomic_admission(monkeypatch):
    trigger = {
        "kind": "turn_end",
        "config": {"provider_kind": "codex", "outcomes": ["complete"]},
    }
    task = task_store.create(
        cwd="/repo",
        name="codex-ingestion-audit-race",
        prompt="audit the completed turn",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
        trigger=trigger,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)
    receipt_id = _turn_end_receipt(task, "receipt-race")
    real_resolve = task_runner._resolve_launch_session
    unadmitted_session_ids: list[str] = []

    async def _update_before_admission(*args, **kwargs):
        session = await real_resolve(*args, **kwargs)
        unadmitted_session_ids.append(session[0]["id"])
        task_store.update(task["id"], {
            "trigger": {"kind": "manual", "config": {}},
        })
        return session

    monkeypatch.setattr(task_runner, "_resolve_launch_session", _update_before_admission)
    try:
        asyncio.run(task_runner.launch_task(
            task["id"],
            coordinator=coordinator,
            source="turn_end_trigger",
            client_id="routine-event:receipt-race",
            event_receipt_id=receipt_id,
        ))
        assert False, "stale trigger admission should fail"
    except task_runner.TaskLaunchError as exc:
        assert exc.status == 409
    assert submitted == []
    assert len(unadmitted_session_ids) == 1
    assert session_manager.get(unadmitted_session_ids[0]) is None


def test_routine_prompt_crosses_real_turn_lifecycle_boundary(monkeypatch):
    task = task_store.create(
        cwd="/repo",
        name="lifecycle-boundary",
        prompt="exercise the real turn boundary",
        model="claude-sonnet-5",
        provider_id="anthropic",
        singleton=True,
    )
    coordinator, submitted = _fake_coordinator(monkeypatch)

    class Provider:
        id = "test-provider"
        KIND = "claude"
        record = {"name": "test-provider", "runner": ""}

    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def drive_cli_run(**_kwargs):
        provider_entered.set()
        await release_provider.wait()
        return {
            "success": True,
            "events": [],
            "session_id": "routine-provider-session",
        }

    monkeypatch.setattr(coordinator, "provider_for_run", lambda *_a, **_kw: Provider())
    monkeypatch.setattr(coordinator, "provider_for_session", lambda *_a, **_kw: Provider())
    monkeypatch.setattr(coordinator.turn_manager, "_drive_cli_run", drive_cli_run)

    async def scenario() -> None:
        await coordinator.turn_manager.lifecycle.bind()
        await coordinator.lifecycle_commands.bind()
        processor = None
        try:
            launched = await task_runner.launch_task(
                task["id"], coordinator=coordinator, source="trigger",
            )
            lifecycle_msg_id = submitted[0]["lifecycle_msg_id"]
            assert uuid.UUID(lifecycle_msg_id)
            coordinator._prompt_queues[launched["session_id"]].put_nowait(None)
            processor = asyncio.create_task(
                coordinator._run_session_processor(launched["session_id"])
            )
            await provider_entered.wait()
            active = coordinator.lifecycle_commands.snapshot(launched["session_id"])
            assert active.phase in {"starting", "running"}
            assert active.identity.lifecycle_message_id == lifecycle_msg_id
            assert active.execution is not None
            release_provider.set()
            await processor
            snapshot = coordinator.lifecycle_commands.snapshot(launched["session_id"])
            assert snapshot.phase == "idle"
            assert snapshot.identity is None
        finally:
            release_provider.set()
            if processor is not None and not processor.done():
                await asyncio.gather(processor, return_exceptions=True)
            await coordinator.lifecycle_commands.close()
            await coordinator.turn_manager.lifecycle.close()

    asyncio.run(scenario())


def test_provisioned_fork_routine_queues_run_on_user_fork(monkeypatch):
    task = task_store.create(
        cwd="/repo",
        name="provisioned-review",
        prompt="review the repo",
        model="claude-sonnet-5",
        provider_id=None,
    )
    base = session_manager.create(
        name="routine base",
        model="claude-sonnet-5",
        provider_id=None,
        cwd="/repo",
        orchestration_mode="native",
        source="internal",
        user_initiated=False,
    )
    session_manager.set_agent_sid(base["id"], "native", "provider-parent-sid")

    class FakeProvisioning:
        @staticmethod
        def resolve_config(spec):
            return spec.build_config()

        @staticmethod
        async def ensure_warm_base(_spec, _cfg):
            return base["id"]

    monkeypatch.setitem(sys.modules, "provisioning", FakeProvisioning)
    monkeypatch.setattr(
        config_store,
        "resolve_internal_llm",
        lambda _key: {"provider_id": None, "model": "claude-sonnet-5", "reasoning_effort": ""},
    )
    coordinator, submitted = _fake_coordinator(monkeypatch, stub_provisioning=False)

    result = asyncio.run(task_runner.launch_task(
        task["id"], coordinator=coordinator, source="trigger",
    ))

    assert result["session_id"] != base["id"]
    fork = session_manager.get(result["session_id"])
    assert fork is not None
    assert fork["parent_session_id"] == base["id"]
    assert submitted[0]["app_session_id"] == result["session_id"]
    assert "Run the saved routine now" in submitted[0]["prompt"]
