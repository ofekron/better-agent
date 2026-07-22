from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-task-memory-mode-")

import session_store
import task_runner
import provisioning
from session_manager import manager as session_manager
from stores import task_store


def teardown_module() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _task(**overrides) -> dict:
    values = {
        "cwd": "/repo",
        "name": "remembering routine",
        "prompt": "review the repo",
        "model": "test-model",
        "provider_id": None,
    }
    values.update(overrides)
    return task_store.create(**values)


def test_every_routine_owns_memory_storage_scope() -> None:
    task = _task()

    assert "session_type" not in task
    assert task_runner._routine_storage_scope(task) == {
        "kind": "routine",
        "routine_id": task["id"],
        "memory": True,
    }
    assert session_store._normalize_storage_scope(
        task_runner._routine_storage_scope(task)
    ) == task_runner._routine_storage_scope(task)


def test_memory_prompt_requires_core_memory_tools_without_exposing_a_path() -> None:
    task = _task()
    prompt = task_runner._routine_memory_prompt(task)

    assert "routine_memory_read" in prompt
    assert "routine_memory_commit" in prompt
    assert "revision_conflict" in prompt
    assert "lock_ops" in prompt
    assert "filesystem" in prompt
    assert "BETTER_AGENT_HOME" not in prompt


def test_memory_mode_forks_its_provisioned_base(monkeypatch) -> None:
    task = _task()
    base = session_manager.create(
        name="memory base",
        model="test-model",
        provider_id=None,
        cwd="/repo",
        orchestration_mode="native",
        source="internal",
        storage_scope=task_runner._routine_storage_scope(task),
    )
    session_manager.set_agent_sid(base["id"], "native", "provider-parent-sid")

    monkeypatch.setattr(provisioning, "resolve_config", lambda spec: spec.build_config())

    async def ensure_warm_base(_spec, _cfg):
        return base["id"]

    monkeypatch.setattr(provisioning, "ensure_warm_base", ensure_warm_base)

    fork, reused = asyncio.run(task_runner._resolve_launch_session(
        task,
        model="test-model",
        provider_id=None,
        reasoning_effort=None,
        runner="",
    ))

    assert reused is False
    assert fork["parent_session_id"] == base["id"]
    assert fork["storage_scope"] == task_runner._routine_storage_scope(task)


def test_singleton_coalescing_does_not_reuse_an_execution_session(monkeypatch) -> None:
    task = _task(singleton=True)
    base = session_manager.create(
        name="singleton base",
        model="test-model",
        provider_id=None,
        cwd="/repo",
        orchestration_mode="native",
        source="internal",
        storage_scope=task_runner._routine_storage_scope(task),
    )
    session_manager.set_agent_sid(base["id"], "native", "provider-parent-sid")
    monkeypatch.setattr(provisioning, "resolve_config", lambda spec: spec.build_config())

    async def ensure_warm_base(_spec, _cfg):
        return base["id"]

    monkeypatch.setattr(provisioning, "ensure_warm_base", ensure_warm_base)

    async def launch_twice():
        first = await task_runner._resolve_launch_session(
            task, model="test-model", provider_id=None, reasoning_effort=None, runner="",
        )
        second = await task_runner._resolve_launch_session(
            task, model="test-model", provider_id=None, reasoning_effort=None, runner="",
        )
        return first, second

    (first, first_reused), (second, second_reused) = asyncio.run(launch_twice())
    assert first_reused is False
    assert second_reused is False
    assert first["id"] != second["id"]
    assert first["parent_session_id"] == base["id"]
    assert second["parent_session_id"] == base["id"]
