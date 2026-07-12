from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path


HOME = Path(tempfile.mkdtemp(prefix="ba-extension-admission-home-"))
PACKAGE = Path(tempfile.mkdtemp(prefix="ba-extension-admission-package-"))
os.environ["BETTER_AGENT_HOME"] = str(HOME)

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

import extension_backend_loader as loader


BACKEND = '''
import asyncio
from fastapi import APIRouter

active = 0
maximum = 0

def create_router(context):
    router = APIRouter()

    @router.get("/work")
    async def work():
        global active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.25)
            return {"maximum": maximum}
        finally:
            active -= 1

    @router.get("/pid")
    async def pid():
        import os
        return {"pid": os.getpid()}

    @router.post("/spawn-child")
    async def spawn_child():
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        return {"pid": child.pid}

    return router
'''


def _spec() -> dict:
    entrypoint = PACKAGE / "backend.py"
    entrypoint.write_text(BACKEND, encoding="utf-8")
    return {
        "extension_id": "test.bounded-extension",
        "install_path": str(PACKAGE),
        "entrypoint": str(entrypoint),
        "entrypoint_kind": "file",
        "source": {"type": "test"},
        "permissions": {},
        "effective_permissions": {},
    }


async def _invoke(spec: dict, path: str):
    return await loader._invoke_backend(
        spec,
        method="GET",
        path=path,
        body_bytes=b"",
        query_b64="",
        safe_headers=[],
        base_url="http://runtime.invalid",
    )


async def test_overload_is_bounded_and_runtime_executor_stays_available() -> None:
    spec = _spec()
    tasks = [asyncio.create_task(_invoke(spec, "work")) for _ in range(24)]
    started = time.monotonic()
    core_result = await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=0.2)
    core_elapsed = time.monotonic() - started
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [item for item in results if not isinstance(item, BaseException)]
    overloads = [
        item
        for item in results
        if isinstance(item, HTTPException) and item.status_code == 503
    ]
    assert core_result == "available"
    assert core_elapsed < 0.2
    assert len(successes) == loader._PER_EXTENSION_MAX_IN_FLIGHT
    assert len(overloads) == len(results) - len(successes)
    assert all(item.headers == {"Retry-After": "1"} for item in overloads)
    maximums = [json.loads(item.body)["maximum"] for item in successes]
    assert max(maximums) <= loader._PER_EXTENSION_MAX_IN_FLIGHT


def test_global_admission_rejects_before_executor_submission() -> None:
    handles = [loader._BackendProc(f"test.global-{index}") for index in range(33)]
    acquired = [handle for handle in handles if loader._acquire_admission(handle)]
    try:
        assert len(acquired) == loader._GLOBAL_MAX_IN_FLIGHT
        assert handles[-1] not in acquired
    finally:
        for handle in acquired:
            loader._release_admission(handle)


async def test_eviction_reaps_and_restart_spawns_a_fresh_process() -> None:
    spec = _spec()
    first = await _invoke(spec, "pid")
    first_pid = json.loads(first.body)["pid"]
    handle = loader._get_handle(spec)
    first_proc = handle.channel.proc

    loader.evict_persistent_backend(spec["extension_id"], wait=True)
    assert first_proc.poll() is not None

    second = await _invoke(spec, "pid")
    second_pid = json.loads(second.body)["pid"]
    assert second_pid != first_pid


async def test_eviction_kills_extension_descendants() -> None:
    from proc_control import process_control

    spec = _spec()
    response = await loader._invoke_backend(
        spec,
        method="POST",
        path="spawn-child",
        body_bytes=b"",
        query_b64="",
        safe_headers=[],
        base_url="http://runtime.invalid",
    )
    child_pid = json.loads(response.body)["pid"]
    assert process_control().pid_alive(child_pid)

    loader.evict_persistent_backend(spec["extension_id"], wait=True)
    assert not process_control().pid_alive(child_pid)


async def test_cancelled_waiter_holds_capacity_until_child_finishes() -> None:
    spec = _spec()
    tasks = [asyncio.create_task(_invoke(spec, "work")) for _ in range(8)]
    await asyncio.sleep(0.05)
    tasks[0].cancel()
    try:
        await tasks[0]
    except asyncio.CancelledError:
        pass

    overloaded = await asyncio.gather(_invoke(spec, "work"), return_exceptions=True)
    assert isinstance(overloaded[0], HTTPException)
    assert overloaded[0].status_code == 503
    await asyncio.gather(*tasks[1:])


def main() -> None:
    try:
        test_global_admission_rejects_before_executor_submission()
        asyncio.run(test_overload_is_bounded_and_runtime_executor_stays_available())
        loader.shutdown_persistent_backends()
        asyncio.run(test_cancelled_waiter_holds_capacity_until_child_finishes())
        loader.shutdown_persistent_backends()
        asyncio.run(test_eviction_kills_extension_descendants())
        loader.shutdown_persistent_backends()
        asyncio.run(test_eviction_reaps_and_restart_spawns_a_fresh_process())
        print("PASS test_extension_backend_admission")
    finally:
        loader.shutdown_persistent_backends()
        shutil.rmtree(HOME, ignore_errors=True)
        shutil.rmtree(PACKAGE, ignore_errors=True)


if __name__ == "__main__":
    main()
