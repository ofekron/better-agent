#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

_TEST_HOME = tempfile.mkdtemp(prefix="ba-shutdown-test-")
atexit.register(shutil.rmtree, _TEST_HOME, True)
os.environ["BETTER_AGENT_HOME"] = _TEST_HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _provider_barriers() -> None:
    import provider

    started = False

    async def admitted_before_submit() -> None:
        nonlocal started
        started = True
        await provider.path_exists_off_loop(Path(__file__))

    admitted = provider.schedule_loop_task(
        asyncio.get_running_loop(),
        admitted_before_submit(),
        name="test-provider-admitted-before-submit",
    )
    assert admitted is not None
    await provider.shutdown_provider_tasks()
    assert admitted.cancelled() and not started

    provider.reopen_provider_tasks()
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> bool:
        entered.set()
        release.wait()
        return True

    task = asyncio.create_task(
        provider.run_provider_poll_off_loop(blocked),
        name="test-provider-direct-running",
    )
    original_known_providers = provider.known_providers
    provider.known_providers = lambda: [
        types.SimpleNamespace(
            _runs={"run": types.SimpleNamespace(complete_task=task)},
        ),
    ]
    await asyncio.to_thread(entered.wait)
    shutdown = asyncio.create_task(provider.shutdown_provider_tasks())
    await asyncio.sleep(0.05)
    assert not shutdown.done(), "shutdown must drain a running poll worker"
    release.set()
    await shutdown
    assert task.done()
    provider.known_providers = original_known_providers

    ran = False

    async def rejected() -> None:
        nonlocal ran
        ran = True

    assert provider.schedule_loop_task(
        asyncio.get_running_loop(), rejected(), name="test-provider-rejected"
    ) is None
    await asyncio.sleep(0)
    assert not ran
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    assert provider.schedule_loop_task(
        closed_loop, rejected(), name="test-provider-closed-loop"
    ) is None
    await asyncio.sleep(0)
    assert not ran


async def _reconcile_barriers() -> None:
    import session_manager as sm

    manager = sm.manager
    manager.bind_reconcile_fn(lambda _root, after_seq=0: [])
    root = "admitted-before-submit"
    manager.mark_reconcile_dirty(root)
    admitted = manager.schedule_reconcile_if_needed(root)
    assert admitted is not None
    await sm.shutdown_reconciles()
    assert admitted.cancelled()

    rejected_root = "rejected-before-consume"
    manager.mark_reconcile_dirty(rejected_root)
    assert manager.schedule_reconcile_if_needed(rejected_root) is None
    assert manager.is_reconcile_dirty(rejected_root)

    sm.reopen_reconciles()
    entered = threading.Event()
    release = threading.Event()

    def reconcile(_root: str, *, after_seq: int = 0) -> list:
        entered.set()
        release.wait()
        return []

    manager.bind_reconcile_fn(reconcile)
    running_root = "running-worker"
    manager.mark_reconcile_dirty(running_root)
    running = manager.schedule_reconcile_if_needed(running_root)
    assert running is not None
    await asyncio.to_thread(entered.wait)
    shutdown = asyncio.create_task(sm.shutdown_reconciles())
    await asyncio.sleep(0.05)
    assert not shutdown.done(), "shutdown must drain a running reconcile worker"
    release.set()
    await shutdown


async def _provider_setup_barriers() -> None:
    import provider_setup

    await provider_setup.provider_setup_status("claude")
    admitted = tuple(provider_setup._STATUS_INFLIGHT.values())
    assert admitted
    await provider_setup.shutdown_provider_setup()
    assert all(task.cancelled() for task in admitted)

    provider_setup.reopen_provider_setup()
    kind = "shutdown-test"
    provider_setup.INSTALLERS[kind] = provider_setup.ProviderInstaller(
        kind=kind,
        label=kind,
        command=sys.executable,
        install_argv=(sys.executable, "-c", "pass"),
        verify_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        prerequisite_argv=(sys.executable, "-c", "pass"),
    )
    await provider_setup.provider_setup_status(kind)
    running = tuple(provider_setup._STATUS_INFLIGHT.values())
    assert running
    for _ in range(100):
        if provider_setup._ACTIVE_PROCESSES:
            break
        await asyncio.sleep(0.01)
    assert provider_setup._ACTIVE_PROCESSES
    await provider_setup.shutdown_provider_setup()
    assert all(task.cancelled() for task in running)
    assert not provider_setup._ACTIVE_PROCESSES
    try:
        await provider_setup.provider_setup_status("claude")
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider setup admitted work after shutdown")

    exited = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await exited.wait()
    await provider_setup._terminate_process(exited)

    provider_setup.reopen_provider_setup()

    async def close_during_started(_event: str, _payload: dict) -> None:
        await provider_setup.shutdown_provider_setup()

    try:
        await provider_setup.start_install(kind, close_during_started)
    except RuntimeError:
        pass
    else:
        raise AssertionError("install crossed the post-broadcast shutdown gate")
    assert kind not in provider_setup._INSTALL_TASKS
    assert kind not in provider_setup._INSTALL_RUNS
    assert not provider_setup._ACTIVE_PROCESSES
    provider_setup.INSTALLERS.pop(kind)


async def main() -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    await _provider_barriers()
    await _reconcile_barriers()
    await _provider_setup_barriers()
    await asyncio.sleep(0)
    assert not unhandled, unhandled
    print("shutdown quiescence: all tests passed")


if __name__ == "__main__":
    asyncio.run(main())
