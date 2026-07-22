from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-routine-memory-")

import routine_memory
from paths import bc_home
from session_manager import manager as session_manager
from stores import task_store


def teardown_module() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _routine(name: str) -> tuple[dict, dict]:
    task = task_store.create(cwd="/repo", name=name, prompt="inspect")
    session = session_manager.create(
        name=name,
        model="test-model",
        provider_id=None,
        cwd="/repo",
        orchestration_mode="native",
        source="internal",
        storage_scope={"kind": "routine", "routine_id": task["id"], "memory": True},
    )
    return task, session


def test_memory_is_bound_to_the_executing_routine_session() -> None:
    first_task, first_session = _routine("first")
    _, second_session = _routine("second")

    initial = asyncio.run(routine_memory.read(first_session["id"]))
    committed = asyncio.run(routine_memory.commit(
        first_session["id"], expected_revision=0, content="first memory", memory_format="text/markdown",
    ))
    second = asyncio.run(routine_memory.read(second_session["id"]))

    assert initial == {"revision": 0, "content": "", "format": "text/plain"}
    assert committed == {"success": True, "revision": 1}
    assert second == {"revision": 0, "content": "", "format": "text/plain"}
    assert asyncio.run(routine_memory.read(first_session["id"]))["content"] == "first memory"
    assert first_task["id"] not in str(second)


def test_non_routine_session_cannot_access_routine_memory() -> None:
    session = session_manager.create(
        name="ordinary",
        model="test-model",
        provider_id=None,
        cwd="/repo",
        orchestration_mode="native",
        source="internal",
    )

    try:
        asyncio.run(routine_memory.read(session["id"]))
    except routine_memory.RoutineMemoryAccessError:
        pass
    else:
        raise AssertionError("ordinary session accessed routine memory")


def test_same_revision_concurrent_commits_produce_one_winner() -> None:
    _, session = _routine("contended")

    async def race() -> list[dict]:
        return await asyncio.gather(
            routine_memory.commit(session["id"], expected_revision=0, content="a", memory_format="text/plain"),
            routine_memory.commit(session["id"], expected_revision=0, content="b", memory_format="text/plain"),
        )

    results = asyncio.run(race())
    winners = [result for result in results if result.get("success") is True]
    conflicts = [result for result in results if result.get("error") == "revision_conflict"]

    assert len(winners) == 1
    assert len(conflicts) == 1
    assert conflicts[0]["current"]["revision"] == 1


def test_same_revision_commits_are_serialized_across_processes() -> None:
    _, session = _routine("cross-process")
    child = r'''import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.environ["ROUTINE_TEST_ROOT"], "backend"))
import routine_memory
if os.environ.get("ROUTINE_TEST_DELAY") == "1":
    original = routine_memory.write_json_durable
    def delayed(path, data):
        time.sleep(0.2)
        original(path, data)
    routine_memory.write_json_durable = delayed
result = asyncio.run(routine_memory.commit(
    os.environ["ROUTINE_TEST_SESSION"],
    expected_revision=0,
    content=os.environ["ROUTINE_TEST_CONTENT"],
    memory_format="text/plain",
))
print(json.dumps(result))
'''
    base_env = {
        **os.environ,
        "BETTER_AGENT_HOME": str(bc_home()),
        "ROUTINE_TEST_ROOT": str(Path(__file__).resolve().parents[2]),
        "ROUTINE_TEST_SESSION": session["id"],
    }
    first = subprocess.Popen(
        [sys.executable, "-c", child],
        env={**base_env, "ROUTINE_TEST_CONTENT": "first", "ROUTINE_TEST_DELAY": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.05)
    second = subprocess.Popen(
        [sys.executable, "-c", child],
        env={**base_env, "ROUTINE_TEST_CONTENT": "second", "ROUTINE_TEST_DELAY": "0"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)
    second_stdout, second_stderr = second.communicate(timeout=10)

    assert first.returncode == 0, first_stderr
    assert second.returncode == 0, second_stderr
    results = [json.loads(first_stdout), json.loads(second_stdout)]
    assert len([result for result in results if result.get("success") is True]) == 1
    assert len([result for result in results if result.get("error") == "revision_conflict"]) == 1


def test_independent_routine_memories_commit_concurrently() -> None:
    _, first = _routine("independent-a")
    _, second = _routine("independent-b")

    async def commit_both() -> list[dict]:
        return await asyncio.gather(
            routine_memory.commit(first["id"], expected_revision=0, content="a", memory_format="text/plain"),
            routine_memory.commit(second["id"], expected_revision=0, content="b", memory_format="text/plain"),
        )

    assert all(result.get("success") is True for result in asyncio.run(commit_both()))


def test_delete_removes_definition_and_memory_without_recreation() -> None:
    task, session = _routine("deleted")
    asyncio.run(routine_memory.commit(
        session["id"], expected_revision=0, content="gone", memory_format="text/plain",
    ))

    removed = asyncio.run(routine_memory.delete_task(task["id"]))

    assert removed is not None
    assert task_store.get(task["id"]) is None
    try:
        asyncio.run(routine_memory.commit(
            session["id"], expected_revision=1, content="recreated", memory_format="text/plain",
        ))
    except routine_memory.RoutineMemoryAccessError:
        pass
    else:
        raise AssertionError("deleted routine memory was recreated")


def test_delete_waits_for_in_flight_commit_and_wins(monkeypatch) -> None:
    task, session = _routine("delete-race")
    write_started = threading.Event()
    allow_write = threading.Event()
    original = routine_memory.write_json_durable

    def paused_write(path, data):
        write_started.set()
        assert allow_write.wait(timeout=5)
        original(path, data)

    monkeypatch.setattr(routine_memory, "write_json_durable", paused_write)

    async def race() -> tuple[dict, dict | None]:
        commit_task = asyncio.create_task(routine_memory.commit(
            session["id"], expected_revision=0, content="committed", memory_format="text/plain",
        ))
        assert await asyncio.to_thread(write_started.wait, 5)
        delete_task = asyncio.create_task(routine_memory.delete_task(task["id"]))
        allow_write.set()
        return await commit_task, await delete_task

    commit_result, delete_result = asyncio.run(race())

    assert commit_result == {"success": True, "revision": 1}
    assert delete_result is not None
    assert task_store.get(task["id"]) is None


def test_failed_atomic_write_preserves_previous_revision(monkeypatch) -> None:
    _, session = _routine("write-failure")
    asyncio.run(routine_memory.commit(
        session["id"], expected_revision=0, content="stable", memory_format="text/plain",
    ))

    def fail_write(_path, _data):
        raise OSError("simulated write failure")

    monkeypatch.setattr(routine_memory, "write_json_durable", fail_write)
    try:
        asyncio.run(routine_memory.commit(
            session["id"], expected_revision=1, content="lost", memory_format="text/plain",
        ))
    except OSError:
        pass
    else:
        raise AssertionError("failed durable write was reported as successful")

    assert asyncio.run(routine_memory.read(session["id"])) == {
        "revision": 1,
        "content": "stable",
        "format": "text/plain",
    }


def test_invalid_persisted_state_fails_closed() -> None:
    task, session = _routine("invalid-state")
    path = routine_memory._state_path(task["id"])
    path.write_text(json.dumps({
        "schema_version": 1,
        "revision": True,
        "content": "invalid",
        "format": "text/plain",
    }), encoding="utf-8")

    try:
        asyncio.run(routine_memory.read(session["id"]))
    except routine_memory.RoutineMemoryAccessError:
        pass
    else:
        raise AssertionError("invalid persisted memory was accepted")


def test_delete_restores_memory_when_task_store_delete_fails(monkeypatch) -> None:
    task, session = _routine("delete-rollback")
    asyncio.run(routine_memory.commit(
        session["id"], expected_revision=0, content="keep", memory_format="text/plain",
    ))

    def fail_delete(_task_id):
        raise OSError("simulated task store failure")

    monkeypatch.setattr(task_store, "delete", fail_delete)
    try:
        asyncio.run(routine_memory.delete_task(task["id"]))
    except OSError:
        pass
    else:
        raise AssertionError("failed task deletion was reported as successful")

    assert asyncio.run(routine_memory.read(session["id"]))["content"] == "keep"
    assert not routine_memory._state_path(task["id"]).with_name("state.deleting.json").exists()


def test_delete_recovers_tombstone_after_unlink_failure(monkeypatch) -> None:
    task, session = _routine("delete-tombstone")
    asyncio.run(routine_memory.commit(
        session["id"], expected_revision=0, content="remove", memory_format="text/plain",
    ))
    tombstone = routine_memory._state_path(task["id"]).with_name("state.deleting.json")
    original_unlink = Path.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == tombstone and not failed:
            failed = True
            raise OSError("simulated tombstone unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    try:
        asyncio.run(routine_memory.delete_task(task["id"]))
    except OSError:
        pass
    else:
        raise AssertionError("tombstone unlink failure was hidden")

    assert task_store.get(task["id"]) is None
    assert tombstone.exists()
    assert asyncio.run(routine_memory.delete_task(task["id"])) is None
    assert not tombstone.exists()


def test_read_recovers_tombstone_before_task_deletion() -> None:
    task, session = _routine("read-tombstone")
    asyncio.run(routine_memory.commit(
        session["id"], expected_revision=0, content="original", memory_format="text/plain",
    ))
    path = routine_memory._state_path(task["id"])
    tombstone = path.with_name("state.deleting.json")
    os.replace(path, tombstone)

    assert asyncio.run(routine_memory.read(session["id"])) == {
        "revision": 1,
        "content": "original",
        "format": "text/plain",
    }
    assert path.exists()
    assert not tombstone.exists()
    assert asyncio.run(routine_memory.commit(
        session["id"], expected_revision=1, content="next", memory_format="text/plain",
    )) == {"success": True, "revision": 2}


def test_distributed_lock_wraps_os_lock_for_commit_and_delete(monkeypatch) -> None:
    task, session = _routine("lock-order")
    events: list[str] = []
    original_acquire = routine_memory.routine_lock.acquire
    original_release = routine_memory.routine_lock.release

    async def lock_ops(*, release=False, **_kwargs):
        events.append("distributed-release" if release else "distributed-acquire")
        return {"success": True, "holder_token": "test-holder"}

    def acquire(namespace, task_id):
        events.append(f"os-acquire:{namespace}")
        return original_acquire(namespace, task_id)

    def release(fd):
        events.append("os-release")
        original_release(fd)

    monkeypatch.setattr(routine_memory.coordination, "lock_ops", lock_ops)
    monkeypatch.setattr(routine_memory.routine_lock, "acquire", acquire)
    monkeypatch.setattr(routine_memory.routine_lock, "release", release)

    asyncio.run(routine_memory.commit(
        session["id"], expected_revision=0, content="locked", memory_format="text/plain",
    ))
    assert events == [
        "distributed-acquire", "os-acquire:memory", "os-release", "distributed-release",
    ]

    events.clear()
    asyncio.run(routine_memory.delete_task(task["id"]))
    assert events == [
        "distributed-acquire", "os-acquire:memory", "os-release", "distributed-release",
    ]


def test_distributed_lock_failure_prevents_memory_mutation(monkeypatch) -> None:
    task, session = _routine("lock-failure")
    os_acquisitions: list[str] = []
    original_acquire = routine_memory.routine_lock.acquire

    async def lock_ops(**_kwargs):
        return {"success": False, "error": "contended"}

    def acquire(namespace, task_id):
        os_acquisitions.append(namespace)
        return original_acquire(namespace, task_id)

    monkeypatch.setattr(routine_memory.coordination, "lock_ops", lock_ops)
    monkeypatch.setattr(routine_memory.routine_lock, "acquire", acquire)

    for operation in (
        lambda: routine_memory.commit(
            session["id"], expected_revision=0, content="blocked", memory_format="text/plain",
        ),
        lambda: routine_memory.delete_task(task["id"]),
    ):
        try:
            asyncio.run(operation())
        except routine_memory.RoutineMemoryBusyError:
            pass
        else:
            raise AssertionError("distributed lock failure did not fail closed")

    assert os_acquisitions == []
    assert task_store.get(task["id"]) is not None
    assert not routine_memory._state_path(task["id"]).exists()
