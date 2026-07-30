"""Real-process proof for bounded provider auth-status admission."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="provider-auth-admission-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME / "state")
BIN_DIR = TEST_HOME / "bin"
CONTROL_DIR = TEST_HOME / "control"
BIN_DIR.mkdir(parents=True)
CONTROL_DIR.mkdir()
os.environ["PATH"] = f"{BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
os.environ["BA_AUTH_PROBE_CONTROL"] = str(CONTROL_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_store  # noqa: E402
import provider_auth  # noqa: E402


PROBE = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["BA_AUTH_PROBE_CONTROL"])
kind = Path(sys.argv[0]).name
identity = Path(
    os.environ.get("CLAUDE_CONFIG_DIR")
    or os.environ.get("CODEX_HOME")
    or "default"
).name
key = f"{kind}-{identity}"
lock_path = root / "state.lock"
state_path = root / "state.json"

def mutate(delta):
    deadline = time.monotonic() + 5
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "active": 0, "max_global": 0, "active_kind": {}, "max_kind": {},
            "starts": [],
        }
        state["active"] += delta
        state["active_kind"][kind] = state["active_kind"].get(kind, 0) + delta
        if delta > 0:
            state["starts"].append(key)
        state["max_global"] = max(state["max_global"], state["active"])
        state["max_kind"][kind] = max(
            state["max_kind"].get(kind, 0),
            state["active_kind"][kind],
        )
        state_path.write_text(json.dumps(state))
    finally:
        lock_path.rmdir()

mutate(1)
(root / f"{key}.started").write_text(str(os.getpid()))
try:
    deadline = time.monotonic() + 20
    while not (root / f"{key}.release").exists():
        if time.monotonic() >= deadline:
            raise SystemExit(3)
        time.sleep(0.01)
finally:
    mutate(-1)
"""


def _install_probe(name: str) -> None:
    path = BIN_DIR / name
    path.write_text(PROBE)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _provider(kind: str, identity: str) -> dict:
    return config_store.add_provider(
        {
            "name": identity,
            "kind": kind,
            "mode": "subscription",
            "config_dir": str(TEST_HOME / identity),
        }
    )


async def _wait_for(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path.name}")
        await asyncio.sleep(0.01)


def _release(kind: str, identity: str) -> None:
    (CONTROL_DIR / f"{kind}-{identity}.release").touch()


def _clear_control() -> None:
    for child in CONTROL_DIR.iterdir():
        if child.is_file():
            child.unlink()


async def _probe(provider: dict) -> bool:
    return await provider_auth._status_authenticated(provider)


async def test_kind_first_admission_and_global_bound() -> None:
    claude_a = _provider("claude", "claude-a")
    claude_b = _provider("claude", "claude-b")
    codex_c = _provider("codex", "codex-c")

    task_a = asyncio.create_task(_probe(claude_a))
    await _wait_for(CONTROL_DIR / "claude-claude-a.started")
    task_b = asyncio.create_task(_probe(claude_b))
    await asyncio.sleep(0)
    task_c = asyncio.create_task(_probe(codex_c))
    await _wait_for(CONTROL_DIR / "codex-codex-c.started")
    assert not (CONTROL_DIR / "claude-claude-b.started").exists()

    _release("claude", "claude-a")
    _release("codex", "codex-c")
    await _wait_for(CONTROL_DIR / "claude-claude-b.started")
    _release("claude", "claude-b")
    assert await asyncio.gather(task_a, task_b, task_c) == [True, True, True]

    state = json.loads((CONTROL_DIR / "state.json").read_text())
    assert state["max_global"] == 2, state
    assert state["max_kind"] == {"claude": 1, "codex": 1}, state
    assert state["starts"][:2] == ["claude-claude-a", "codex-codex-c"], state


async def test_cancellation_and_shutdown_leave_no_children() -> None:
    _clear_control()
    claude_a = _provider("claude", "cancel-a")
    claude_b = _provider("claude", "cancel-b")
    codex_c = _provider("codex", "cancel-c")

    task_a = asyncio.create_task(_probe(claude_a))
    await _wait_for(CONTROL_DIR / "claude-cancel-a.started")
    waiting_kind = asyncio.create_task(_probe(claude_b))
    await asyncio.sleep(0)
    waiting_kind.cancel()
    await asyncio.gather(waiting_kind, return_exceptions=True)

    task_c = asyncio.create_task(_probe(codex_c))
    await _wait_for(CONTROL_DIR / "codex-cancel-c.started")
    pids = [
        int((CONTROL_DIR / name).read_text())
        for name in ("claude-cancel-a.started", "codex-cancel-c.started")
    ]
    await provider_auth.shutdown_status_probes()
    await asyncio.gather(task_a, task_c, return_exceptions=True)

    assert not provider_auth._status_tasks
    assert not provider_auth._status_procs
    assert not provider_auth._status_cleanup_tasks
    assert all(task.done() for task in (task_a, waiting_kind, task_c))
    assert all(not provider_auth.process_control().pid_alive(pid) for pid in pids)

    provider_auth.reopen_status_probes()
    _release("claude", "cancel-b")
    retry = asyncio.create_task(_probe(claude_b))
    await _wait_for(CONTROL_DIR / "claude-cancel-b.started")
    assert await retry is True


async def test_cancel_after_kind_before_global_releases_kind() -> None:
    _clear_control()
    provider_auth._status_global_semaphore = asyncio.Semaphore(1)
    provider_auth._status_kind_semaphores.clear()
    claude = _provider("claude", "global-holder")
    codex = _provider("codex", "global-waiter")

    holder = asyncio.create_task(_probe(claude))
    await _wait_for(CONTROL_DIR / "claude-global-holder.started")
    waiter = asyncio.create_task(_probe(codex))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    _release("claude", "global-holder")
    assert await holder is True

    _release("codex", "global-waiter")
    retry = asyncio.create_task(_probe(codex))
    await _wait_for(CONTROL_DIR / "codex-global-waiter.started")
    assert await retry is True
    provider_auth._status_global_semaphore = asyncio.Semaphore(
        provider_auth._STATUS_GLOBAL_CONCURRENCY
    )
    provider_auth._status_kind_semaphores.clear()


async def test_cancel_during_spawn_reaps_real_child() -> None:
    _clear_control()
    provider = _provider("claude", "spawn-cancel")
    original_spawn = provider_auth._spawn
    original_reap = provider_auth._kill_and_reap_status_proc
    spawned = asyncio.Event()
    return_spawn = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    captured: list[asyncio.subprocess.Process] = []

    async def gated_spawn(record: dict, suffix: str):
        proc = await original_spawn(record, suffix)
        assert proc is not None
        captured.append(proc)
        spawned.set()
        await return_spawn.wait()
        return proc

    async def gated_reap(proc: asyncio.subprocess.Process) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        await original_reap(proc)

    provider_auth._spawn = gated_spawn
    provider_auth._kill_and_reap_status_proc = gated_reap
    try:
        task = asyncio.create_task(_probe(provider))
        await spawned.wait()
        task.cancel()
        return_spawn.set()
        await cleanup_started.wait()
        task.cancel()
        release_cleanup.set()
        await asyncio.gather(task, return_exceptions=True)
        assert captured[0].returncode is not None
        assert not provider_auth.process_control().pid_alive(captured[0].pid)
        assert not provider_auth._status_procs
        assert not provider_auth._status_cleanup_tasks
    finally:
        provider_auth._spawn = original_spawn
        provider_auth._kill_and_reap_status_proc = original_reap


async def test_failed_reap_retains_ownership_until_shutdown_retry() -> None:
    _clear_control()
    provider = _provider("claude", "reap-retry")
    original_reap = provider_auth._kill_and_reap_status_proc
    failed_once = False

    async def fail_once(proc: asyncio.subprocess.Process) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            provider_auth._kill_proc(proc)
            raise RuntimeError("controlled reap timeout")
        await original_reap(proc)

    provider_auth._kill_and_reap_status_proc = fail_once
    try:
        task = asyncio.create_task(_probe(provider))
        await _wait_for(CONTROL_DIR / "claude-reap-retry.started")
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], RuntimeError), result
        assert provider_auth._status_procs
        assert provider_auth._status_cleanup_tasks
    finally:
        provider_auth._kill_and_reap_status_proc = original_reap

    await provider_auth.shutdown_status_probes()
    assert not provider_auth._status_procs
    assert not provider_auth._status_cleanup_tasks
    provider_auth.reopen_status_probes()


async def test_timeout_and_completion_shutdown_race_reap() -> None:
    _clear_control()
    provider = _provider("claude", "timeout")
    original_timeout = provider_auth._STATUS_TIMEOUT_SECONDS
    provider_auth._STATUS_TIMEOUT_SECONDS = 0.5
    try:
        assert await _probe(provider) is False
        started = CONTROL_DIR / "claude-timeout.started"
        await _wait_for(started)
        assert not provider_auth.process_control().pid_alive(
            int(started.read_text())
        )
        assert not provider_auth._status_procs
    finally:
        provider_auth._STATUS_TIMEOUT_SECONDS = original_timeout

    _clear_control()
    provider_auth.reopen_status_probes()
    provider = _provider("codex", "completion-race")
    task = asyncio.create_task(_probe(provider))
    started = CONTROL_DIR / "codex-completion-race.started"
    await _wait_for(started)
    pid = int(started.read_text())
    _release("codex", "completion-race")
    await asyncio.gather(
        task,
        provider_auth.shutdown_status_probes(),
        return_exceptions=True,
    )
    assert not provider_auth.process_control().pid_alive(pid)
    assert not provider_auth._status_procs
    assert not provider_auth._status_tasks
    assert not provider_auth._status_cleanup_tasks
    provider_auth.reopen_status_probes()


async def test_lifespan_continues_after_status_reap_failure() -> None:
    import app_lifecycle
    import model_catalog_refresh
    import requirement_prewarm

    events: list[str] = []
    originals = {
        "offline": app_lifecycle.offline_actions_api.close_prompt_handoffs,
        "catalog": model_catalog_refresh.shutdown,
        "requirement": requirement_prewarm.shutdown_thread_vector_projection,
        "lag": app_lifecycle.lag_incident_queue.stop,
        "marketplace": app_lifecycle.marketplace_bridge_api.stop,
        "status": provider_auth.shutdown_status_probes,
        "interactive": provider_auth.shutdown_all,
        "downstream": app_lifecycle.extension_api.shutdown_hot_path_executors,
    }

    async def noop_async() -> None:
        return None

    async def fail_status() -> None:
        events.append("status")
        raise RuntimeError("controlled retained status child")

    def stop_interactive() -> None:
        events.append("interactive")

    async def downstream_sentinel() -> None:
        events.append("downstream")
        raise LookupError("downstream sentinel")

    app_lifecycle.offline_actions_api.close_prompt_handoffs = lambda: None
    model_catalog_refresh.shutdown = noop_async
    requirement_prewarm.shutdown_thread_vector_projection = noop_async
    app_lifecycle.lag_incident_queue.stop = noop_async
    app_lifecycle.marketplace_bridge_api.stop = noop_async
    provider_auth.shutdown_status_probes = fail_status
    provider_auth.shutdown_all = stop_interactive
    app_lifecycle.extension_api.shutdown_hot_path_executors = downstream_sentinel
    app_lifecycle._STARTUP_ORCHESTRATOR_TASK = None
    app_lifecycle._model_catalog_unsubscribe = None
    try:
        try:
            await app_lifecycle.on_shutdown()
        except LookupError as exc:
            assert str(exc) == "downstream sentinel"
        else:
            raise AssertionError("downstream shutdown sentinel was not reached")
        assert events == ["status", "interactive", "downstream"], events
    finally:
        app_lifecycle.offline_actions_api.close_prompt_handoffs = originals["offline"]
        model_catalog_refresh.shutdown = originals["catalog"]
        requirement_prewarm.shutdown_thread_vector_projection = originals["requirement"]
        app_lifecycle.lag_incident_queue.stop = originals["lag"]
        app_lifecycle.marketplace_bridge_api.stop = originals["marketplace"]
        provider_auth.shutdown_status_probes = originals["status"]
        provider_auth.shutdown_all = originals["interactive"]
        app_lifecycle.extension_api.shutdown_hot_path_executors = originals["downstream"]


async def main() -> None:
    _install_probe("claude")
    _install_probe("codex")
    try:
        await test_kind_first_admission_and_global_bound()
        await test_cancellation_and_shutdown_leave_no_children()
        await test_cancel_after_kind_before_global_releases_kind()
        await test_cancel_during_spawn_reaps_real_child()
        await test_failed_reap_retains_ownership_until_shutdown_retry()
        await test_timeout_and_completion_shutdown_race_reap()
        await test_lifespan_continues_after_status_reap_failure()
    finally:
        await provider_auth.shutdown_status_probes()
        provider_auth.shutdown_all()
        shutil.rmtree(TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    print("provider auth probe admission integration test passed")
