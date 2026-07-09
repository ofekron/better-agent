from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-task-runner-collapse-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import task_runner
import config_store
from orchestrator import Coordinator
from session_manager import manager as session_manager
from stores import task_store


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _fake_coordinator(monkeypatch):
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
    return coordinator, submitted


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
    q = coordinator._prompt_queues[first["session_id"]]
    assert q.qsize() == 1


def test_provisioned_fork_routine_queues_run_on_user_fork(monkeypatch):
    task = task_store.create(
        cwd="/repo",
        name="provisioned-review",
        prompt="review the repo",
        model="claude-sonnet-5",
        provider_id=None,
        session_type="provisioned_fork",
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
    coordinator, submitted = _fake_coordinator(monkeypatch)

    result = asyncio.run(task_runner.launch_task(
        task["id"], coordinator=coordinator, source="trigger",
    ))

    assert result["session_id"] != base["id"]
    fork = session_manager.get(result["session_id"])
    assert fork is not None
    assert fork["parent_session_id"] == base["id"]
    assert submitted[0]["app_session_id"] == result["session_id"]
    assert "Run the saved routine now" in submitted[0]["prompt"]
