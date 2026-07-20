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
from task_session_types import PROVISIONED_FORK_WITH_MEMORY


def teardown_module() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _task(**overrides) -> dict:
    values = {
        "cwd": "/repo",
        "name": "remembering routine",
        "prompt": "review the repo",
        "model": "test-model",
        "provider_id": None,
        "session_type": PROVISIONED_FORK_WITH_MEMORY,
    }
    values.update(overrides)
    return task_store.create(**values)


def test_memory_mode_is_valid_and_owns_distinct_storage_scope() -> None:
    task = _task()

    assert task["session_type"] == PROVISIONED_FORK_WITH_MEMORY
    assert task_runner._routine_storage_scope(task) == {
        "kind": "routine",
        "routine_id": task["id"],
        "memory": True,
    }
    assert session_store._normalize_storage_scope(
        task_runner._routine_storage_scope(task)
    ) == task_runner._routine_storage_scope(task)


def test_memory_prompt_assigns_a_folder_without_prescribing_technology() -> None:
    task = _task()
    prompt = task_runner._routine_memory_prompt(task)

    assert prompt == (
        "You own the persistent memory directory at "
        f"routine-memory/{task['id']} under the directory specified by the "
        "BETTER_AGENT_HOME environment variable. "
        "Create and maintain your own memory layer there. "
        "Choose how it works and what it contains."
    )
    for prescription in ("database", "sqlite", "json", "markdown", "schema"):
        assert prescription not in prompt.lower()
    assert task_runner._routine_memory_prompt({**task, "session_type": "provisioned_fork"}) == ""


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
    ))

    assert reused is False
    assert fork["parent_session_id"] == base["id"]
    assert fork["storage_scope"] == task_runner._routine_storage_scope(task)


def test_singleton_does_not_reuse_a_session_without_memory_scope() -> None:
    task = _task(singleton=True)
    old = session_manager.create(
        name="old singleton",
        model="test-model",
        provider_id=None,
        cwd="/repo",
        orchestration_mode="native",
        source="internal",
        storage_scope={"kind": "routine", "routine_id": task["id"]},
    )
    task_store.record_run(task["id"], old["id"])
    task = task_store.get(task["id"])

    assert task is not None
    assert task_runner._resolve_singleton_session(task) is None
    assert task_store.get(task["id"])["singleton_session_id"] is None
