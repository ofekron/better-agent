#!/usr/bin/env python3
import asyncio
import dataclasses
import json
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_lifecycle import (
    BootstrapSnapshot,
    LifecycleOutcome,
    MutationResult,
    RunLifecycleCoordinator,
    bind_runtime_owner_loop,
    ensure_runtime_owner,
)
from provider import StreamEvent


async def deterministic_state_machine() -> None:
    owner_loop = asyncio.get_running_loop()
    lifecycle: RunLifecycleCoordinator[object] = RunLifecycleCoordinator(owner_loop)
    foreign_result = []

    def foreign_loop() -> None:
        async def run() -> None:
            foreign_result.append(await lifecycle.admit("dual-loop"))
        asyncio.run(run())

    thread = threading.Thread(target=foreign_loop)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0)
    thread.join()
    accepted = foreign_result[0]
    assert accepted.accepted and accepted.token is not None
    assert (await lifecycle.admit("dual-loop")).outcome is LifecycleOutcome.DUPLICATE

    token = accepted.token
    stale = dataclasses.replace(token, nonce="not-the-owner")
    value = object()
    assert (await lifecycle.publish(stale, value)).outcome is LifecycleOutcome.STALE
    assert (await lifecycle.publish(token, value)).accepted
    assert (await lifecycle.retire(token, object())).outcome is LifecycleOutcome.STALE
    assert (await lifecycle.retire(token, value)).accepted

    reserved_cancel = await lifecycle.admit("reserved-cancel")
    assert reserved_cancel.token is not None
    cancelled_reservation = await lifecycle.cancel("reserved-cancel")
    assert cancelled_reservation.token == reserved_cancel.token
    assert cancelled_reservation.value is None
    assert (await lifecycle.publish(reserved_cancel.token, object())).outcome is LifecycleOutcome.STALE

    payload = {"nested": [{"mutable": [1, 2]}]}
    frozen = BootstrapSnapshot.create(token, b"seed", payload)
    payload["nested"][0]["mutable"].append(3)
    assert frozen.values["nested"][0]["mutable"] == (1, 2)

    cancelled = asyncio.create_task(lifecycle.admit("cancelled"))
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    assert (await lifecycle.admit("cancelled")).accepted

    inventory = await lifecycle.shutdown()
    assert len(inventory.reserved) == 1
    assert await lifecycle.shutdown() is inventory
    assert (await lifecycle.admit("late")).outcome is LifecycleOutcome.SHUTDOWN


def runtime_generation_replaces_stale_provider_state() -> None:
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    calling_loop = asyncio.new_event_loop()
    try:
        bind_runtime_owner_loop(first_loop)
        lifecycle = ensure_runtime_owner(None, calling_loop)
        assert lifecycle.owner_loop is first_loop
        first_loop.run_until_complete(lifecycle.admit("survives-restart"))
        first_loop.close()

        bind_runtime_owner_loop(second_loop)
        replacement = ensure_runtime_owner(lifecycle, calling_loop)
        assert replacement is not lifecycle
        assert replacement.owner_loop is second_loop
        result = second_loop.run_until_complete(replacement.admit("survives-restart"))
        assert result.accepted

        prebound = RunLifecycleCoordinator(calling_loop)
        assert ensure_runtime_owner(prebound, calling_loop).owner_loop is second_loop

        active_foreign = RunLifecycleCoordinator(calling_loop)
        calling_loop.run_until_complete(active_foreign.admit("active"))
        try:
            ensure_runtime_owner(active_foreign, calling_loop)
        except RuntimeError as exc:
            assert "different owner loop" in str(exc)
        else:
            raise AssertionError("active foreign lifecycle must fail closed")
    finally:
        if not first_loop.is_closed():
            first_loop.close()
        second_loop.close()
        calling_loop.close()


async def real_descendant_inventory() -> None:
    lifecycle: RunLifecycleCoordinator[subprocess.Popen] = RunLifecycleCoordinator(
        asyncio.get_running_loop()
    )
    admitted = await lifecycle.admit("tree")
    assert admitted.token is not None
    child = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"],
        start_new_session=True,
    )
    try:
        assert (await lifecycle.publish(admitted.token, child)).accepted
        inventory = await lifecycle.shutdown()
        assert inventory.published[0].value is child
    finally:
        if child.poll() is None:
            import os
            import signal
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


async def claude_shutdown_terminates_tree_and_cleans() -> None:
    import os
    import provider_claude
    from provider import Provider

    root = Path(tempfile.mkdtemp(prefix="claude-lifecycle-tree-"))
    provider_claude._runs_root = lambda: root
    run_dir = root / "tree-run"
    run_dir.mkdir()
    descendant_pid_path = run_dir / "descendant.pid"
    script = (
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(descendant_pid_path)!r}).write_text(str(p.pid)); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    deadline = time.monotonic() + 5
    while not descendant_pid_path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    descendant_pid = int(descendant_pid_path.read_text())

    provider = provider_claude.ClaudeProvider.__new__(provider_claude.ClaudeProvider)
    provider._runs = {}
    provider._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    provider._lifecycle_runs = {}
    provider._lifecycle_spawn_tasks = set()
    provider._recovery_attach_pending = set()
    provider._recovery_pending_states = {}
    rs = SimpleNamespace(
        run_id="tree-run", run_dir=run_dir, popen=child, released=asyncio.Event()
    )
    provider._runs[rs.run_id] = rs
    admitted = await provider._lifecycle.admit(rs.run_id)
    assert admitted.token is not None
    record = provider_claude.ClaudeLifecycleRecord("tree-run", "cleanup", child.pid, str(run_dir))
    rs.lifecycle_token = admitted.token
    rs.lifecycle_record = record
    provider._lifecycle_runs[record.cleanup_nonce] = rs
    assert (await provider._lifecycle.publish(admitted.token, record)).accepted
    safety_fallback = False
    try:
        await provider.shutdown_lifecycle()
        child.wait(timeout=5)
        assert child.poll() is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("Claude shutdown left descendant alive")
        assert not run_dir.exists()
        assert provider._runs == {}
        retained = (await provider._lifecycle.shutdown()).published[0]
        assert retained.value == dataclasses.replace(record)
    finally:
        if child.poll() is None:
            safety_fallback = True
            os.killpg(child.pid, 9)
            child.wait(timeout=5)
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    assert not safety_fallback, "test safety fallback had to kill Claude tree"


async def claude_pre_publish_cancel_cleans_spawn_result() -> None:
    import os
    import provider_claude

    root = Path(tempfile.mkdtemp(prefix="claude-lifecycle-cancel-"))
    provider_claude._runs_root = lambda: root
    provider = provider_claude.ClaudeProvider.__new__(provider_claude.ClaudeProvider)
    provider._runs = {}
    provider._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    provider._lifecycle_runs = {}
    provider._lifecycle_spawn_tasks = set()
    provider._recovery_attach_pending = set()
    provider._recovery_pending_states = {}
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    box = {}

    def blocked_spawn(**kwargs):
        run_dir = root / kwargs["run_id"]
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        rs = SimpleNamespace(
            run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
            released=asyncio.Event(), session_id=None,
        )
        box["rs"] = rs
        spawn_entered.set()
        release_spawn.wait(5)
        return rs

    provider._spawn_run = blocked_spawn
    provider._bootstrap_run = lambda _rs: asyncio.sleep(0)
    kwargs = {"run_id": "cancel-race", "session_id": None, "fork": False}
    task = asyncio.create_task(provider._admit_and_spawn(kwargs))
    provider._lifecycle_spawn_tasks.add(task)
    task.add_done_callback(provider._lifecycle_spawn_tasks.discard)
    assert await asyncio.to_thread(spawn_entered.wait, 5)
    shutdown_task = asyncio.create_task(provider.shutdown_lifecycle())
    await asyncio.sleep(0)
    release_spawn.set()
    result = await asyncio.gather(task, shutdown_task, return_exceptions=True)
    assert result[0] is None
    rs = box["rs"]
    rs.popen.wait(timeout=5)
    assert rs.popen.poll() is not None
    assert not rs.run_dir.exists()
    assert provider._runs == {}
    assert not provider._lifecycle_spawn_tasks
    import shutil
    shutil.rmtree(root, ignore_errors=True)


async def claude_bootstrap_failure_cleans_published_run() -> None:
    import provider_claude

    root = Path(tempfile.mkdtemp(prefix="claude-lifecycle-bootstrap-fail-"))
    provider_claude._runs_root = lambda: root
    provider = provider_claude.ClaudeProvider.__new__(provider_claude.ClaudeProvider)
    provider._runs = {}
    provider._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    provider._lifecycle_runs = {}
    provider._lifecycle_spawn_tasks = set()
    provider._recovery_attach_pending = set()
    provider._recovery_pending_states = {}
    box = {}

    def spawn(**kwargs):
        run_dir = root / kwargs["run_id"]
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        rs = SimpleNamespace(
            run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
            released=asyncio.Event(), session_id=None,
            lifecycle_token=None, lifecycle_record=None,
        )
        box["rs"] = rs
        return rs

    async def fail_bootstrap(_rs):
        raise LookupError("bootstrap-original-error")

    provider._spawn_run = spawn
    provider._bootstrap_run = fail_bootstrap
    result = await asyncio.gather(
        provider._admit_and_spawn({"run_id": "bootstrap-fail", "session_id": None, "fork": False}),
        return_exceptions=True,
    )
    assert isinstance(result[0], LookupError)
    assert str(result[0]) == "bootstrap-original-error"
    rs = box["rs"]
    rs.popen.wait(timeout=5)
    assert rs.popen.poll() is not None
    assert not rs.run_dir.exists()
    assert provider._runs == {}
    assert provider._lifecycle_runs == {}
    assert await provider._lifecycle.snapshot() == ()
    assert not provider._lifecycle_spawn_tasks
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def _bare_codex_provider(provider_codex, loop):
    provider = provider_codex.CodexProvider.__new__(provider_codex.CodexProvider)
    provider._runs = {}
    provider._lifecycle = RunLifecycleCoordinator(loop)
    provider._lifecycle_runs = {}
    provider._lifecycle_spawn_tasks = set()
    provider._recovery_attach_pending = set()
    provider._recovery_pending_states = {}
    return provider


def _bare_openai_provider(provider_openai, loop):
    provider = provider_openai.OpenAIProvider.__new__(provider_openai.OpenAIProvider)
    provider._runs = {}
    provider._lifecycle = RunLifecycleCoordinator(loop)
    provider._lifecycle_runs = {}
    provider._lifecycle_spawn_tasks = set()
    provider._recovery_pending_states = {}
    return provider


async def openai_pre_publish_shutdown_and_reload_contracts() -> None:
    import os
    import provider_openai
    import shutil

    for terminate_runs in (True, False):
        root = Path(tempfile.mkdtemp(prefix="openai-lifecycle-spawn-"))
        original_runs_root = provider_openai._runs_root
        provider_openai._runs_root = lambda _root=root: _root
        provider = _bare_openai_provider(provider_openai, asyncio.get_running_loop())
        entered = threading.Event()
        release = threading.Event()
        box = {}

        def blocked_spawn(**kwargs):
            run_dir = root / kwargs["run_id"]
            run_dir.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            rs = SimpleNamespace(
                run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
                lifecycle_token=None, lifecycle_record=None,
            )
            box["rs"] = rs
            entered.set()
            release.wait(5)
            return rs

        provider._spawn_run = blocked_spawn
        provider._bootstrap_run = lambda _rs: asyncio.sleep(0)
        task = asyncio.create_task(provider._admit_and_spawn({"run_id": "openai-race"}))
        provider._lifecycle_spawn_tasks.add(task)
        task.add_done_callback(provider._lifecycle_spawn_tasks.discard)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            shutdown = asyncio.create_task(
                provider.shutdown_lifecycle(terminate_runs=terminate_runs)
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task, shutdown)
            rs = box["rs"]
            if terminate_runs:
                rs.popen.wait(timeout=5)
                assert rs.popen.poll() is not None
                assert not rs.run_dir.exists()
                assert provider._runs == {}
            else:
                assert rs.popen.poll() is None
                assert provider._runs[rs.run_id] is rs
                inventory = await provider._lifecycle.shutdown()
                assert inventory.published[0].value.run_id == rs.run_id
        finally:
            release.set()
            rs = box.get("rs")
            if rs is not None and rs.popen.poll() is None:
                os.killpg(rs.popen.pid, 9)
                rs.popen.wait(timeout=5)
            provider_openai._runs_root = original_runs_root
            shutil.rmtree(root, ignore_errors=True)


async def openai_bootstrap_failure_cleans_published_process() -> None:
    import provider_openai
    import shutil

    root = Path(tempfile.mkdtemp(prefix="openai-lifecycle-bootstrap-fail-"))
    original_runs_root = provider_openai._runs_root
    provider_openai._runs_root = lambda: root
    provider = _bare_openai_provider(provider_openai, asyncio.get_running_loop())
    box = {}

    def spawn(**kwargs):
        run_dir = root / kwargs["run_id"]
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        rs = SimpleNamespace(
            run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
            lifecycle_token=None, lifecycle_record=None,
        )
        box["rs"] = rs
        return rs

    async def fail_bootstrap(_rs):
        raise LookupError("openai-bootstrap-original-error")

    provider._spawn_run = spawn
    provider._bootstrap_run = fail_bootstrap
    try:
        result = await asyncio.gather(
            provider._admit_and_spawn({"run_id": "openai-bootstrap-fail"}),
            return_exceptions=True,
        )
        assert isinstance(result[0], LookupError)
        assert str(result[0]) == "openai-bootstrap-original-error"
        rs = box["rs"]
        rs.popen.wait(timeout=5)
        assert rs.popen.poll() is not None
        assert not rs.run_dir.exists()
        assert provider._runs == {}
        assert provider._lifecycle_runs == {}
        assert await provider._lifecycle.snapshot() == ()
    finally:
        rs = box.get("rs")
        if rs is not None and rs.popen.poll() is None:
            import os
            os.killpg(rs.popen.pid, 9)
            rs.popen.wait(timeout=5)
        provider_openai._runs_root = original_runs_root
        shutil.rmtree(root, ignore_errors=True)


async def openai_unpublished_failures_cleanup_exactly_once() -> None:
    import provider_openai

    for recovered, cleanup_fails in (
        (False, False), (True, False), (False, True), (True, True),
    ):
        provider = _bare_openai_provider(provider_openai, asyncio.get_running_loop())
        counts = {"terminate": 0, "cleanup": 0, "rollback": 0}
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        run_dir = Path(tempfile.mkdtemp(prefix="openai-exact-once-"))
        rs = SimpleNamespace(
            run_id="openai-exact-once", run_dir=run_dir, popen=child,
            lifecycle_token=None, lifecycle_record=None,
        )
        original_terminate = provider_openai.terminate_failed_run_process
        original_cleanup = provider._cleanup_lifecycle_artifacts
        original_rollback = provider._lifecycle.rollback

        def counted_terminate(value):
            counts["terminate"] += 1
            original_terminate(value)
            if cleanup_fails:
                raise RuntimeError("secondary terminate failure")

        def counted_cleanup(value):
            counts["cleanup"] += 1
            provider._recovery_pending_states.pop(value.run_id, None)
            provider._runs.pop(value.run_id, None)
            if cleanup_fails:
                raise RuntimeError("secondary cleanup failure")

        async def counted_rollback(token):
            counts["rollback"] += 1
            result = await original_rollback(token)
            if cleanup_fails:
                raise RuntimeError("secondary rollback failure")
            return result

        provider_openai.terminate_failed_run_process = counted_terminate
        provider._cleanup_lifecycle_artifacts = counted_cleanup
        provider._lifecycle.rollback = counted_rollback
        original_error = OSError("seed failed") if recovered else RuntimeError(
            "publish transport failed"
        )
        try:
            if recovered:
                provider._recovery_pending_states[rs.run_id] = rs

                def fail_seed(_rs):
                    raise original_error

                provider._write_backend_state = fail_seed
                result = await asyncio.gather(
                    provider._admit_recovered_run(rs), return_exceptions=True
                )
                assert isinstance(result[0], OSError)
                assert result[0] is original_error
                assert str(result[0]) == "seed failed"
            else:
                provider._spawn_run = lambda **_kwargs: rs
                provider._bootstrap_run = lambda _rs: asyncio.sleep(0)

                async def reject_publish(_token, _record):
                    if cleanup_fails:
                        raise original_error
                    return MutationResult(LifecycleOutcome.STALE)

                provider._lifecycle.publish = reject_publish
                result = await asyncio.gather(
                    provider._admit_and_spawn({"run_id": rs.run_id}),
                    return_exceptions=True,
                )
                assert isinstance(result[0], RuntimeError)
                if cleanup_fails:
                    assert result[0] is original_error
                    assert str(result[0]) == "publish transport failed"
                else:
                    assert str(result[0]) == "OpenAI run publish rejected: stale"
            assert counts == {"terminate": 1, "cleanup": 1, "rollback": 1}
            assert provider._runs == {}
            assert provider._recovery_pending_states == {}
        finally:
            provider_openai.terminate_failed_run_process = original_terminate
            if child.poll() is None:
                import os
                os.killpg(child.pid, 9)
                child.wait(timeout=5)
            import shutil
            shutil.rmtree(run_dir, ignore_errors=True)


async def openai_actual_bootstrap_persistence_failure_is_terminal() -> None:
    import provider_openai
    import shutil

    root = Path(tempfile.mkdtemp(prefix="openai-bootstrap-persist-"))
    original_runs_root = provider_openai._runs_root
    provider_openai._runs_root = lambda: root
    provider = _bare_openai_provider(provider_openai, asyncio.get_running_loop())
    run_dir = root / "persist-fail"
    run_dir.mkdir()
    (run_dir / "state.json").write_text('{"session_id":"openai-sid"}')
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    queue = asyncio.Queue()
    rs = SimpleNamespace(
        run_id="persist-fail", run_dir=run_dir, popen=child, mode="native",
        app_session_id="app", queue=queue, session_id=None, processed_line=0,
        started_at="", cancelled=False, persist_to="app", target_message_id=None,
        turn_run_id=None, lifecycle_token=None, lifecycle_record=None,
    )
    provider._spawn_run = lambda **_kwargs: rs

    def fail_commit(_rs):
        raise OSError("backend state commit failed")

    provider._write_backend_state = fail_commit
    try:
        result = await asyncio.gather(
            provider._admit_and_spawn({"run_id": rs.run_id}),
            return_exceptions=True,
        )
        assert isinstance(result[0], OSError)
        assert str(result[0]) == "backend state commit failed"
        child.wait(timeout=5)
        assert child.poll() is not None
        assert not run_dir.exists()
        assert provider._runs == {}
        assert provider._lifecycle_runs == {}
        assert await provider._lifecycle.snapshot() == ()
        emitted = [queue.get_nowait(), queue.get_nowait()]
        assert [event.type for event in emitted] == ["error", "complete"]
    finally:
        if child.poll() is None:
            import os
            os.killpg(child.pid, 9)
            child.wait(timeout=5)
        provider_openai._runs_root = original_runs_root
        shutil.rmtree(root, ignore_errors=True)


async def openai_live_and_recovered_cancel_cleanup() -> None:
    import provider_openai
    import shutil

    for recovered in (False, True):
        root = Path(tempfile.mkdtemp(prefix="openai-cancel-"))
        original_runs_root = provider_openai._runs_root
        provider_openai._runs_root = lambda _root=root: _root
        provider = _bare_openai_provider(provider_openai, asyncio.get_running_loop())
        run_id = "recovered-cancel" if recovered else "live-cancel"
        run_dir = root / run_id
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        rs = SimpleNamespace(
            run_id=run_id, run_dir=run_dir, popen=child, mode="native",
            app_session_id="app", queue=asyncio.Queue(), session_id="sid",
            processed_line=0, started_at="", cancelled=False, persist_to="app",
            target_message_id=None, turn_run_id=None, lifecycle_token=None,
            lifecycle_record=None, tailer=None,
        )
        provider._write_backend_state = lambda _rs: None
        provider._bootstrap_run = lambda _rs: asyncio.sleep(0)
        try:
            if recovered:
                provider._recovery_pending_states[run_id] = rs
                await provider._admit_recovered_run(rs)
            else:
                provider._spawn_run = lambda **_kwargs: rs
                await provider._admit_and_spawn({"run_id": run_id})
            assert provider.cancel_run(run_id)
            child.wait(timeout=5)
            deadline = asyncio.get_running_loop().time() + 5
            while (provider._runs or provider._lifecycle_runs) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert child.poll() is not None
            assert not run_dir.exists()
            assert provider._runs == {}
            assert provider._lifecycle_runs == {}
            assert await provider._lifecycle.snapshot() == ()
        finally:
            if child.poll() is None:
                import os
                os.killpg(child.pid, 9)
                child.wait(timeout=5)
            provider_openai._runs_root = original_runs_root
            shutil.rmtree(root, ignore_errors=True)


async def remote_ack_and_cancel_races() -> None:
    import provider_remote
    import shutil

    root = Path(tempfile.mkdtemp(prefix="remote-lifecycle-"))
    original_root = provider_remote.runs_root
    original_spawn = provider_remote.node_link.send_spawn_run
    original_cancel = provider_remote.node_link.send_cancel_run
    provider_remote.runs_root = lambda: root
    cancel_calls = []
    try:
        for scenario in (
            "accepted", "accepted-cancel", "cancel-before-ack", "disconnect-before-ack",
        ):
            proxy = provider_remote.RemoteProviderProxy(f"node-{scenario}")
            loop = asyncio.get_running_loop()
            proxy._lifecycle = RunLifecycleCoordinator(loop)
            state = provider_remote._RemoteRunState(
                run_id=scenario, run_dir=root / scenario, mode="native",
                app_session_id="app", queue=asyncio.Queue(), node_id=proxy.node_id,
                loop=loop, persist_to="app",
                lifecycle_nonce=f"nonce-{scenario}",
            )
            state.popen = provider_remote._FakePopen(state)
            provider_remote._proxies[proxy.node_id] = proxy

            async def fake_spawn(_node_id, _payload):
                return None

            async def fake_cancel(node_id, run_id, **_kwargs):
                cancel_calls.append((node_id, run_id))
                return True

            provider_remote.node_link.send_spawn_run = fake_spawn
            provider_remote.node_link.send_cancel_run = fake_cancel
            task = asyncio.create_task(proxy._admit_send_publish(
                state, {"run_id": scenario}, root_id="root", cwd="/tmp", source=None,
            ))
            while scenario not in proxy._pending_acks:
                await asyncio.sleep(0)
            pending_state = json.loads(
                (root / scenario / "backend_state.json").read_text(encoding="utf-8")
            )
            assert pending_state["lifecycle_state"] == "pending"
            assert pending_state["lifecycle_nonce"] == proxy._pending_nonces[scenario]
            if scenario == "accepted":
                nonce = proxy._pending_nonces[scenario]
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="accepted",
                    data={"lifecycle_nonce": nonce},
                )
                await task
                assert proxy._runs[scenario] is state
                assert state.lifecycle_record is not None
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="session_discovered",
                    data={"lifecycle_nonce": "stale", "session_id": "wrong"},
                )
                assert state.queue.empty()
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="session_discovered",
                    data={"lifecycle_nonce": nonce, "session_id": "exact"},
                )
                discovered = state.queue.get_nowait()
                assert discovered.type == "session_discovered"
                assert discovered.data["session_id"] == "exact"
                await proxy.shutdown_lifecycle(terminate_runs=False)
                assert cancel_calls == []
            elif scenario == "accepted-cancel":
                nonce = proxy._pending_nonces[scenario]
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="accepted", data={"lifecycle_nonce": nonce},
                )
                await task
                assert proxy.cancel_run(scenario)
                await asyncio.sleep(0.05)
                cancelling = json.loads(
                    (root / scenario / "backend_state.json").read_text(encoding="utf-8")
                )
                assert cancelling["lifecycle_state"] == "cancelling"
                assert (root / scenario).exists()
                assert scenario in proxy._runs
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="error",
                    data={"lifecycle_nonce": "stale", "error": "wrong generation"},
                )
                assert scenario in proxy._runs
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="error",
                    data={"lifecycle_nonce": nonce, "error": "remote run cancelled"},
                )
                assert scenario not in proxy._runs
                assert (root / scenario / "reconciled.marker").exists()
            elif scenario == "cancel-before-ack":
                assert proxy.cancel_run(scenario)
                await asyncio.sleep(0)
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="accepted", data={"lifecycle_nonce": "stale"},
                )
                await provider_remote._on_run_control(
                    node_id=proxy.node_id, run_id=scenario,
                    control_type="error", data={
                        "lifecycle_nonce": proxy._pending_nonces[scenario],
                        "error": "remote run cancelled before acceptance",
                    },
                )
                await asyncio.gather(task, return_exceptions=True)
                assert proxy._runs == {}
                assert cancel_calls.count((proxy.node_id, scenario)) == 1
            else:
                nonce = proxy._pending_nonces[scenario]
                await provider_remote._on_node_state(proxy.node_id, "disconnected")
                result = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result[0], provider_remote.node_link.NodeOffline)
                assert proxy._runs == {}
                assert cancel_calls.count((proxy.node_id, scenario)) == 1
                durable = json.loads(
                    (root / scenario / "backend_state.json").read_text(encoding="utf-8")
                )
                assert durable["lifecycle_state"] == "cancelling"
                assert durable["lifecycle_nonce"] == nonce
                assert (root / scenario).exists()
    finally:
        provider_remote.runs_root = original_root
        provider_remote.node_link.send_spawn_run = original_spawn
        provider_remote.node_link.send_cancel_run = original_cancel
        shutil.rmtree(root, ignore_errors=True)


async def node_handler_cancel_between_receipt_and_context() -> None:
    import node_rpc_handlers
    import runs_dir
    import shutil

    root = Path(tempfile.mkdtemp(prefix="node-handler-gate-"))
    original_provider = node_rpc_handlers.default_provider
    original_flush = node_rpc_handlers.session_manager.flush_pending_persists
    original_root = runs_dir.runs_root
    original_atomic = runs_dir.atomic_write_json
    write_entered = threading.Event()
    release_write = threading.Event()
    release_terminal = asyncio.Event()
    controls = []

    class FakeProvider:
        def __init__(self):
            self.cancel_calls = 0
            self.queue = None

        def start_run(self, **kwargs):
            self.queue = kwargs["queue"]
            return None

        async def await_run_started(self, _run_id):
            return None

        def cancel_run(self, _run_id):
            self.cancel_calls += 1
            async def terminal():
                await release_terminal.wait()
                await self.queue.put(StreamEvent(
                    "error", {"error": "provider observed cancellation"},
                ))
            asyncio.get_running_loop().create_task(terminal())
            return True

    class FakeClient:
        async def send_run_control(self, **frame):
            controls.append(frame)

    provider = FakeProvider()

    def blocked_atomic(path, data):
        if path.name == "remote_ctx.json":
            write_entered.set()
            release_write.wait(5)
        return original_atomic(path, data)

    node_rpc_handlers.default_provider = lambda: provider
    node_rpc_handlers.session_manager.flush_pending_persists = lambda: None
    runs_dir.runs_root = lambda: root
    runs_dir.atomic_write_json = blocked_atomic
    msg = {
        "run_id": "node-gated", "root_id": "root", "cwd": "/tmp",
        "app_session_id": "app", "prompt": "x", "lifecycle_nonce": "nonce-1",
    }
    client = FakeClient()
    try:
        spawn = asyncio.create_task(node_rpc_handlers.handle_spawn_run(client, msg))
        assert await asyncio.to_thread(write_entered.wait, 5)
        await node_rpc_handlers.handle_cancel_run(client, {
            "run_id": "node-gated", "lifecycle_nonce": "nonce-1",
        })
        release_write.set()
        await spawn
        assert provider.cancel_calls >= 1
        assert not any(frame.get("control_type") == "accepted" for frame in controls)
        persisted = json.loads(
            (root / "node-gated" / "remote_ctx.json").read_text(encoding="utf-8")
        )
        assert persisted == {
            "root_id": "root", "worker_agent_session_id": "app", "cwd": "/tmp",
            "lifecycle_nonce": "nonce-1", "lifecycle_state": "cancelling",
        }
        release_terminal.set()
        deadline = time.monotonic() + 3
        while not controls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert controls[0]["data"]["error"] == "provider observed cancellation"
        assert "node-gated" not in node_rpc_handlers._reservation_by_run
        assert json.loads(
            (root / "node-gated" / "remote_ctx.json").read_text(encoding="utf-8")
        )["lifecycle_state"] == "terminal"
    finally:
        release_write.set()
        node_rpc_handlers.default_provider = original_provider
        node_rpc_handlers.session_manager.flush_pending_persists = original_flush
        runs_dir.runs_root = original_root
        runs_dir.atomic_write_json = original_atomic
        node_rpc_handlers._reservation_by_run.pop("node-gated", None)
        node_rpc_handlers._ctx_by_run.pop("node-gated", None)
        shutil.rmtree(root, ignore_errors=True)


async def node_rehook_restores_nonce_and_cancelling_state() -> None:
    import node_rpc_handlers
    import runs_dir
    import shutil

    root = Path(tempfile.mkdtemp(prefix="node-rehook-lifecycle-"))
    run_id = "rehook-cancelling"
    run_dir = root / run_id
    run_dir.mkdir()
    (run_dir / "remote_ctx.json").write_text(json.dumps({
        "root_id": "root", "worker_agent_session_id": "worker", "cwd": "/tmp",
        "lifecycle_nonce": "rehook-nonce", "lifecycle_state": "cancelling",
    }))
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "session_discovered", "data": {"session_id": "sid-r"}})
        + "\n"
        + json.dumps({"type": "error", "data": {"error": "original terminal error"}})
        + "\n"
    )
    original_root = runs_dir.runs_root
    original_provider = node_rpc_handlers.default_provider
    original_compute_jsonl_path = node_rpc_handlers.compute_jsonl_path
    controls = []

    class Provider:
        def __init__(self):
            self.cancelled = []

        def cancel_run(self, value):
            self.cancelled.append(value)
            return True

    class Client:
        async def send_run_control(self, **frame):
            controls.append(frame)

    provider = Provider()
    runs_dir.runs_root = lambda: root
    node_rpc_handlers.default_provider = lambda: provider
    node_rpc_handlers.compute_jsonl_path = lambda _cwd, _sid: None
    try:
        await node_rpc_handlers.handle_rehook_run(Client(), {
            "run_id": run_id, "lifecycle_nonce": "stale",
        })
        assert run_id not in node_rpc_handlers._ctx_by_run
        await node_rpc_handlers.handle_rehook_run(Client(), {
            "run_id": run_id, "lifecycle_nonce": "rehook-nonce",
        })
        deadline = time.monotonic() + 3
        while len(controls) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert provider.cancelled == [run_id]
        assert controls == [
            {
                "run_id": run_id, "control_type": "session_discovered",
                "data": {"session_id": "sid-r", "lifecycle_nonce": "rehook-nonce"},
            },
            {
                "run_id": run_id, "control_type": "error",
                "data": {
                    "error": "original terminal error",
                    "lifecycle_nonce": "rehook-nonce",
                },
            },
        ]
    finally:
        ctx = node_rpc_handlers._ctx_by_run.pop(run_id, None)
        if ctx is not None and ctx.drain_task is not None:
            ctx.drain_task.cancel()
        node_rpc_handlers._reservation_by_run.pop(run_id, None)
        node_rpc_handlers.default_provider = original_provider
        node_rpc_handlers.compute_jsonl_path = original_compute_jsonl_path
        runs_dir.runs_root = original_root
        shutil.rmtree(root, ignore_errors=True)


async def remote_recovery_retries_cancel_before_rehook() -> None:
    import node_link
    import run_recovery
    import shutil

    root = Path(tempfile.mkdtemp(prefix="remote-cancel-retry-"))
    run_dir = root / "retry-run"
    run_dir.mkdir()
    state = {
        "run_id": "retry-run", "provider_id": "remote:node-r", "node_id": "node-r",
        "root_id": "root", "app_session_id": "app", "persist_to": "app",
        "mode": "native", "started_at": "2026-01-01T00:00:00",
        "lifecycle_nonce": "retry-nonce", "lifecycle_state": "cancelling",
    }
    calls = []
    original_cancel = node_link.send_cancel_run
    original_rehook = node_link.send_rehook_run

    async def cancel(node_id, run_id, *, lifecycle_nonce):
        calls.append(("cancel", node_id, run_id, lifecycle_nonce))
        return True

    async def rehook(node_id, run_id, *, lifecycle_nonce):
        calls.append(("rehook", node_id, run_id, lifecycle_nonce))

    node_link.send_cancel_run = cancel
    node_link.send_rehook_run = rehook
    try:
        result = await run_recovery._prepare_remote_desc(
            "node-r", run_dir, state,
            {"exists": True, "alive": True, "complete": None},
        )
        assert result is None
        assert calls == [
            ("cancel", "node-r", "retry-run", "retry-nonce"),
            ("rehook", "node-r", "retry-run", "retry-nonce"),
        ]
        assert not (run_dir / "reconciled.marker").exists()
    finally:
        node_link.send_cancel_run = original_cancel
        node_link.send_rehook_run = original_rehook
        shutil.rmtree(root, ignore_errors=True)


async def remote_primary_crash_waits_for_exact_terminal() -> None:
    import provider_remote
    import shutil

    root = Path(tempfile.mkdtemp(prefix="remote-primary-crash-"))
    original_root = provider_remote.runs_root
    run_id = "crash-before-terminal"
    run_dir = root / run_id
    run_dir.mkdir()
    state = {
        "run_id": run_id, "provider_id": "remote:node-c", "node_id": "node-c",
        "root_id": "root", "app_session_id": "app", "persist_to": "app",
        "mode": "native", "started_at": "2026-01-01T00:00:00",
        "lifecycle_nonce": "crash-nonce", "lifecycle_state": "cancelling",
    }
    (run_dir / "backend_state.json").write_text(json.dumps(state), encoding="utf-8")
    provider_remote.runs_root = lambda: root
    provider_remote._proxies.pop("node-c", None)
    try:
        await provider_remote._on_run_control(
            node_id="node-c", run_id=run_id, control_type="error",
            data={"lifecycle_nonce": "stale", "error": "stale terminal"},
        )
        assert not (run_dir / "complete.json").exists()
        await provider_remote._on_run_control(
            node_id="node-c", run_id=run_id, control_type="error",
            data={"lifecycle_nonce": "crash-nonce", "error": "original terminal"},
        )
        await asyncio.sleep(0)
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        assert complete["error"] == "original terminal"
        assert not (run_dir / "reconciled.marker").exists()
    finally:
        provider_remote._proxies.pop("node-c", None)
        provider_remote.runs_root = original_root
        shutil.rmtree(root, ignore_errors=True)


async def remote_public_start_boundary_records_immediate_cancel() -> None:
    import provider_remote
    import session_manager
    import shutil

    root = Path(tempfile.mkdtemp(prefix="remote-immediate-cancel-"))
    original_root = provider_remote.runs_root
    original_connection = provider_remote.node_store.get_connection
    original_spawn = provider_remote.node_link.send_spawn_run
    original_cancel = provider_remote.node_link.send_cancel_run
    original_get = session_manager.manager.get
    original_root_id = session_manager.manager._root_id_for
    cancel_calls = []
    proxy = provider_remote.RemoteProviderProxy("node-immediate")

    class Conn:
        runs = {}

    async def spawn(_node_id, payload):
        await provider_remote._on_run_control(
            node_id=proxy.node_id, run_id=payload["run_id"],
            control_type="accepted",
            data={"lifecycle_nonce": payload["lifecycle_nonce"]},
        )

    async def cancel(node_id, run_id, *, lifecycle_nonce):
        cancel_calls.append((node_id, run_id, lifecycle_nonce))
        return True

    provider_remote.runs_root = lambda: root
    provider_remote.node_store.get_connection = lambda _node_id: Conn()
    provider_remote.node_link.send_spawn_run = spawn
    provider_remote.node_link.send_cancel_run = cancel
    session_manager.manager.get = lambda _sid: {"id": "app"}
    session_manager.manager._root_id_for = lambda _sid: "root"
    provider_remote._proxies[proxy.node_id] = proxy
    try:
        loop = asyncio.get_running_loop()
        proxy.start_run(
            run_id="immediate", prompt="x", cwd="/tmp", loop=loop,
            queue=asyncio.Queue(), model=None, reasoning_effort=None,
            session_id=None, mode="native", app_session_id="app",
        )
        assert proxy._pending_states["immediate"].cancelled is False
        assert (root / "immediate" / "backend_state.json").exists()
        assert proxy.cancel_run("immediate")
        assert proxy._pending_states["immediate"].cancelled is True
        deadline = time.monotonic() + 3
        while "immediate" not in proxy._runs and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert cancel_calls == [(
            "node-immediate", "immediate",
            proxy._runs["immediate"].lifecycle_token.nonce,
        )]
        durable = json.loads(
            (root / "immediate" / "backend_state.json").read_text(encoding="utf-8")
        )
        assert durable["lifecycle_state"] == "cancelling"
        nonce = durable["lifecycle_nonce"]
        await provider_remote._on_run_control(
            node_id=proxy.node_id, run_id="immediate", control_type="error",
            data={"lifecycle_nonce": nonce, "error": "cancelled"},
        )
        assert "immediate" not in proxy._runs
    finally:
        provider_remote._proxies.pop(proxy.node_id, None)
        provider_remote.runs_root = original_root
        provider_remote.node_store.get_connection = original_connection
        provider_remote.node_link.send_spawn_run = original_spawn
        provider_remote.node_link.send_cancel_run = original_cancel
        session_manager.manager.get = original_get
        session_manager.manager._root_id_for = original_root_id
        shutil.rmtree(root, ignore_errors=True)


async def node_cancel_reconstructs_accepting_after_restart() -> None:
    import node_rpc_handlers
    import runs_dir
    import shutil

    root = Path(tempfile.mkdtemp(prefix="node-cancel-restart-"))
    run_id = "accepting-restart"
    run_dir = root / run_id
    run_dir.mkdir()
    (run_dir / "remote_ctx.json").write_text(json.dumps({
        "root_id": "root", "worker_agent_session_id": "app", "cwd": "/tmp",
        "lifecycle_nonce": "restart-nonce", "lifecycle_state": "accepting",
    }), encoding="utf-8")
    original_root = runs_dir.runs_root
    original_provider = node_rpc_handlers.default_provider

    class Provider:
        def __init__(self):
            self.calls = []

        def cancel_run(self, value):
            self.calls.append(value)
            return True

    provider = Provider()
    runs_dir.runs_root = lambda: root
    node_rpc_handlers.default_provider = lambda: provider
    node_rpc_handlers._ctx_by_run.pop(run_id, None)
    node_rpc_handlers._reservation_by_run.pop(run_id, None)
    try:
        await node_rpc_handlers.handle_cancel_run(object(), {
            "run_id": run_id, "lifecycle_nonce": "stale",
        })
        assert provider.calls == []
        await node_rpc_handlers.handle_cancel_run(object(), {
            "run_id": run_id, "lifecycle_nonce": "restart-nonce",
        })
        assert provider.calls == [run_id]
        durable = json.loads((run_dir / "remote_ctx.json").read_text(encoding="utf-8"))
        assert durable["lifecycle_state"] == "cancelling"
        assert node_rpc_handlers._reservation_by_run[run_id].state == "cancelling"
    finally:
        node_rpc_handlers._ctx_by_run.pop(run_id, None)
        node_rpc_handlers._reservation_by_run.pop(run_id, None)
        node_rpc_handlers.default_provider = original_provider
        runs_dir.runs_root = original_root
        shutil.rmtree(root, ignore_errors=True)


async def node_accepted_send_failure_stays_recoverable() -> None:
    import node_rpc_handlers
    import runs_dir
    import shutil

    root = Path(tempfile.mkdtemp(prefix="node-accepted-send-fail-"))
    run_id = "accepted-send-fail"
    original_root = runs_dir.runs_root
    original_provider = node_rpc_handlers.default_provider
    original_flush = node_rpc_handlers.session_manager.flush_pending_persists

    class Provider:
        def __init__(self):
            self.cancel_calls = []

        def start_run(self, **_kwargs):
            return None

        async def await_run_started(self, _run_id):
            return None

        def cancel_run(self, value):
            self.cancel_calls.append(value)
            return True

    class BrokenAcceptedClient:
        async def send_run_control(self, **frame):
            if frame["control_type"] == "accepted":
                raise ConnectionError("accepted frame lost")

    provider = Provider()
    runs_dir.runs_root = lambda: root
    node_rpc_handlers.default_provider = lambda: provider
    node_rpc_handlers.session_manager.flush_pending_persists = lambda: None
    try:
        await node_rpc_handlers.handle_spawn_run(BrokenAcceptedClient(), {
            "run_id": run_id, "root_id": "root", "cwd": "/tmp",
            "app_session_id": "app", "prompt": "x",
            "lifecycle_nonce": "accepted-fail-nonce",
        })
        durable = json.loads(
            (root / run_id / "remote_ctx.json").read_text(encoding="utf-8")
        )
        assert durable["lifecycle_state"] == "cancelling"
        assert provider.cancel_calls == [run_id]
        ctx = node_rpc_handlers._ctx_by_run.pop(run_id)
        if ctx.drain_task is not None:
            ctx.drain_task.cancel()
            await asyncio.gather(ctx.drain_task, return_exceptions=True)
        node_rpc_handlers._reservation_by_run.pop(run_id, None)
        await node_rpc_handlers.handle_cancel_run(object(), {
            "run_id": run_id, "lifecycle_nonce": "accepted-fail-nonce",
        })
        assert provider.cancel_calls == [run_id, run_id]
        assert json.loads(
            (root / run_id / "remote_ctx.json").read_text(encoding="utf-8")
        )["lifecycle_state"] == "cancelling"
    finally:
        ctx = node_rpc_handlers._ctx_by_run.pop(run_id, None)
        if ctx is not None and ctx.drain_task is not None:
            ctx.drain_task.cancel()
        node_rpc_handlers._reservation_by_run.pop(run_id, None)
        node_rpc_handlers.session_manager.flush_pending_persists = original_flush
        node_rpc_handlers.default_provider = original_provider
        runs_dir.runs_root = original_root
        shutil.rmtree(root, ignore_errors=True)


async def remote_reconnect_recovery_has_one_owner() -> None:
    import provider_remote
    from pathlib import Path

    calls = []
    import run_recovery
    original = run_recovery.integrate_remote_runs_for_node

    async def counted(node_id, *args, **kwargs):
        calls.append((node_id, args, kwargs))

    run_recovery.integrate_remote_runs_for_node = counted
    try:
        await provider_remote._on_node_state("one-owner", "connected")
        assert calls == []
        main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8"
        )
        assert main_source.count("run_recovery.integrate_remote_runs_for_node(node_id)") == 1
    finally:
        run_recovery.integrate_remote_runs_for_node = original


async def codex_pre_publish_shutdown_cleans_spawn_result() -> None:
    import provider_codex

    root = Path(tempfile.mkdtemp(prefix="codex-lifecycle-cancel-"))
    original_runs_root = provider_codex._runs_root
    provider_codex._runs_root = lambda: root
    provider = _bare_codex_provider(provider_codex, asyncio.get_running_loop())
    event_loop_thread = threading.get_ident()
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    box = {}

    def blocked_spawn(**kwargs):
        assert threading.get_ident() != event_loop_thread
        run_dir = root / kwargs["run_id"]
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        rs = SimpleNamespace(
            run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
            lifecycle_token=None, lifecycle_record=None,
        )
        box["rs"] = rs
        spawn_entered.set()
        release_spawn.wait(5)
        return rs

    provider._spawn_run = blocked_spawn
    provider._bootstrap_run = lambda _rs: asyncio.sleep(0)
    task = asyncio.create_task(provider._admit_and_spawn({"run_id": "cancel-race"}))
    provider._lifecycle_spawn_tasks.add(task)
    task.add_done_callback(provider._lifecycle_spawn_tasks.discard)
    try:
        assert await asyncio.to_thread(spawn_entered.wait, 5)
        shutdown_task = asyncio.create_task(provider.shutdown_lifecycle())
        await asyncio.sleep(0)
        release_spawn.set()
        result = await asyncio.gather(task, shutdown_task, return_exceptions=True)
        assert result[0] is None
        rs = box["rs"]
        rs.popen.wait(timeout=5)
        assert rs.popen.poll() is not None
        assert not rs.run_dir.exists()
        assert provider._runs == {}
        assert not provider._lifecycle_spawn_tasks
    finally:
        release_spawn.set()
        rs = box.get("rs")
        if rs is not None and rs.popen.poll() is None:
            import os
            os.killpg(rs.popen.pid, 9)
            rs.popen.wait(timeout=5)
        provider_codex._runs_root = original_runs_root
        import shutil
        shutil.rmtree(root, ignore_errors=True)


async def codex_reload_adopts_blocked_spawn_without_killing() -> None:
    import os
    import provider_codex

    root = Path(tempfile.mkdtemp(prefix="codex-lifecycle-reload-"))
    original_runs_root = provider_codex._runs_root
    provider_codex._runs_root = lambda: root
    provider = _bare_codex_provider(provider_codex, asyncio.get_running_loop())
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    box = {}

    def blocked_spawn(**kwargs):
        run_dir = root / kwargs["run_id"]
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        rs = SimpleNamespace(
            run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
            lifecycle_token=None, lifecycle_record=None,
        )
        box["rs"] = rs
        spawn_entered.set()
        release_spawn.wait(5)
        return rs

    provider._spawn_run = blocked_spawn
    provider._bootstrap_run = lambda _rs: asyncio.sleep(0)
    task = asyncio.create_task(provider._admit_and_spawn({"run_id": "reload-race"}))
    provider._lifecycle_spawn_tasks.add(task)
    task.add_done_callback(provider._lifecycle_spawn_tasks.discard)
    try:
        assert await asyncio.to_thread(spawn_entered.wait, 5)
        shutdown_task = asyncio.create_task(
            provider.shutdown_lifecycle(terminate_runs=False)
        )
        await asyncio.sleep(0)
        release_spawn.set()
        await asyncio.gather(task, shutdown_task)
        rs = box["rs"]
        assert rs.popen.poll() is None
        assert rs.run_dir.exists()
        assert provider._runs["reload-race"] is rs
        inventory = await provider._lifecycle.shutdown()
        assert inventory.published[0].value.run_id == "reload-race"
    finally:
        release_spawn.set()
        rs = box.get("rs")
        if rs is not None and rs.popen.poll() is None:
            os.killpg(rs.popen.pid, 9)
            rs.popen.wait(timeout=5)
        provider_codex._runs_root = original_runs_root
        import shutil
        shutil.rmtree(root, ignore_errors=True)


async def recovered_attach_pre_admission_shutdown_kills_owned_process() -> None:
    import os
    import provider_claude
    import provider_codex
    import provider_openai

    for module, provider_type, kind in (
        (provider_codex, provider_codex.CodexProvider, "codex"),
        (provider_claude, provider_claude.ClaudeProvider, "claude"),
        (provider_openai, provider_openai.OpenAIProvider, "openai"),
    ):
        root = Path(tempfile.mkdtemp(prefix=f"{kind}-recover-pre-admit-"))
        original_runs_root = module._runs_root
        module._runs_root = lambda _root=root: _root
        run_id = f"{kind}-recover-pending"
        run_dir = root / run_id
        run_dir.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        record = {"id": f"{kind}-test", "kind": kind}
        if kind == "openai":
            record.update(base_url="http://127.0.0.1:1/v1", api_key="test")
        owner = provider_type(record)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_admit = owner._admit_recovered_run

        async def gated_admit(rs, _original=original_admit):
            entered.set()
            await release.wait()
            await _original(rs)

        owner._admit_recovered_run = gated_admit
        try:
            assert owner.attach_recovered_run(
                desc={
                    "run_id": run_id, "pid": child.pid, "mode": "native",
                    "app_session_id": "app", "provider_id": f"{kind}-test",
                },
                queue=asyncio.Queue(), loop=asyncio.get_running_loop(),
            )
            await entered.wait()
            shutdown = asyncio.create_task(owner.shutdown_lifecycle(terminate_runs=True))
            await asyncio.sleep(0)
            release.set()
            await shutdown
            child.wait(timeout=5)
            assert child.poll() is not None
            assert not run_dir.exists()
            assert owner._runs == {}
            assert owner._recovery_pending_states == {}
        finally:
            release.set()
            if child.poll() is None:
                os.killpg(child.pid, 9)
                child.wait(timeout=5)
            module._runs_root = original_runs_root
            import shutil
            shutil.rmtree(root, ignore_errors=True)


async def codex_bootstrap_failure_cleans_published_tree() -> None:
    import os
    import provider_codex

    root = Path(tempfile.mkdtemp(prefix="codex-lifecycle-bootstrap-fail-"))
    original_runs_root = provider_codex._runs_root
    provider_codex._runs_root = lambda: root
    provider = _bare_codex_provider(provider_codex, asyncio.get_running_loop())
    box = {}

    def spawn(**kwargs):
        run_dir = root / kwargs["run_id"]
        run_dir.mkdir()
        descendant_pid_path = run_dir / "descendant.pid"
        script = (
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"pathlib.Path({str(descendant_pid_path)!r}).write_text(str(p.pid)); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        deadline = time.monotonic() + 5
        while not descendant_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        rs = SimpleNamespace(
            run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
            lifecycle_token=None, lifecycle_record=None,
        )
        box["rs"] = rs
        box["descendant_pid"] = int(descendant_pid_path.read_text())
        return rs

    async def fail_bootstrap(_rs):
        raise LookupError("codex-bootstrap-original-error")

    provider._spawn_run = spawn
    provider._bootstrap_run = fail_bootstrap
    try:
        result = await asyncio.gather(
            provider._admit_and_spawn({"run_id": "bootstrap-fail"}),
            return_exceptions=True,
        )
        assert isinstance(result[0], LookupError)
        assert str(result[0]) == "codex-bootstrap-original-error"
        rs = box["rs"]
        rs.popen.wait(timeout=5)
        assert rs.popen.poll() is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(box["descendant_pid"], 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("Codex bootstrap cleanup left descendant alive")
        assert not rs.run_dir.exists()
        assert provider._runs == {}
        assert provider._lifecycle_runs == {}
        assert await provider._lifecycle.snapshot() == ()
    finally:
        rs = box.get("rs")
        if rs is not None and rs.popen.poll() is None:
            os.killpg(rs.popen.pid, 9)
            rs.popen.wait(timeout=5)
        provider_codex._runs_root = original_runs_root
        import shutil
        shutil.rmtree(root, ignore_errors=True)


async def fugu_lifecycle_behaves_with_sakana_config() -> None:
    import os
    import provider_codex
    from provider_fugu import FuguProvider

    root = Path(tempfile.mkdtemp(prefix="fugu-lifecycle-"))
    original_runs_root = provider_codex._runs_root
    provider_codex._runs_root = lambda: root

    def provider():
        return FuguProvider({"id": "fugu-test", "kind": "fugu"})

    def spawn_for(owner, box, *, fail=False):
        def spawn(**kwargs):
            run_dir = root / kwargs["run_id"]
            run_dir.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            rs = provider_codex.RunState(
                run_id=kwargs["run_id"], run_dir=run_dir, popen=child,
                mode="native", app_session_id="app", queue=asyncio.Queue(),
            )
            box["rs"] = rs
            return rs

        async def bootstrap(_rs):
            if fail:
                raise LookupError("fugu-bootstrap-failure")

        owner._spawn_run = spawn
        owner._bootstrap_run = bootstrap

    live = provider()
    live._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    box = {}
    spawn_for(live, box)
    try:
        overrides = live.codex_config_overrides(model="fugu-ultra")
        assert 'model_provider="sakana"' in overrides
        assert 'model="fugu-ultra"' in overrides
        assert "features.image_generation=false" in overrides

        await live._admit_and_spawn({"run_id": "fugu-live"})
        assert live._runs["fugu-live"] is box["rs"]
        assert (await live._lifecycle.get("fugu-live")).run_id == "fugu-live"
        assert live.cancel_run("fugu-live")
        deadline = time.monotonic() + 5
        while live._runs and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert live._runs == {}
        box["rs"].popen.wait(timeout=5)
        assert not box["rs"].run_dir.exists()

        failed = provider()
        failed._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
        failed_box = {}
        spawn_for(failed, failed_box, fail=True)
        result = await asyncio.gather(
            failed._admit_and_spawn({"run_id": "fugu-fail"}),
            return_exceptions=True,
        )
        assert isinstance(result[0], LookupError)
        failed_box["rs"].popen.wait(timeout=5)
        assert failed._runs == {}
        assert not failed_box["rs"].run_dir.exists()

        recovered = provider()
        recovered._bootstrap_run = lambda _rs: asyncio.sleep(0)
        recovery_dir = root / "fugu-recovered"
        recovery_dir.mkdir()
        recovery_child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        attached = recovered.attach_recovered_run(
            desc={
                "run_id": "fugu-recovered", "pid": recovery_child.pid,
                "mode": "native", "app_session_id": "app",
                "provider_id": "fugu-test",
            },
            queue=asyncio.Queue(), loop=asyncio.get_running_loop(),
        )
        assert attached
        deadline = time.monotonic() + 5
        while "fugu-recovered" not in recovered._runs and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert recovered._runs["fugu-recovered"].lifecycle_record.run_id == "fugu-recovered"
        assert recovered._lifecycle_runs
        await recovered.shutdown_lifecycle(terminate_runs=True)
        recovery_child.wait(timeout=5)
        assert recovered._runs == {}
        assert not recovery_dir.exists()
    finally:
        for current in (live, locals().get("failed"), locals().get("recovered")):
            if current is None:
                continue
            for rs in list(current._runs.values()):
                if rs.popen.poll() is None:
                    os.killpg(rs.popen.pid, 9)
                    rs.popen.wait(timeout=5)
        provider_codex._runs_root = original_runs_root
        import shutil
        shutil.rmtree(root, ignore_errors=True)


async def shared_provider_shutdown_hook_is_single_and_policy_aware() -> None:
    import provider as provider_module

    calls = []

    class Owned:
        def __init__(self, provider_id):
            self.id = provider_id

        async def shutdown_lifecycle(self, *, terminate_runs=True):
            calls.append((self.id, terminate_runs))

    original = dict(provider_module._PROVIDER_CACHE)
    try:
        provider_module._PROVIDER_CACHE.clear()
        provider_module._PROVIDER_CACHE.update({"a": Owned("a"), "b": Owned("b")})
        assert await provider_module.shutdown_provider_lifecycles(terminate_runs=False) == 0
        assert calls == [("a", False), ("b", False)]
        calls.clear()
        assert await provider_module.shutdown_provider_lifecycle("a")
        assert calls == [("a", True)]
        assert not await provider_module.shutdown_provider_lifecycle("missing")
    finally:
        provider_module._PROVIDER_CACHE.clear()
        provider_module._PROVIDER_CACHE.update(original)


async def main() -> None:
    await asyncio.to_thread(runtime_generation_replaces_stale_provider_state)
    await deterministic_state_machine()
    await real_descendant_inventory()
    await claude_shutdown_terminates_tree_and_cleans()
    await claude_pre_publish_cancel_cleans_spawn_result()
    await claude_bootstrap_failure_cleans_published_run()
    await codex_pre_publish_shutdown_cleans_spawn_result()
    await codex_reload_adopts_blocked_spawn_without_killing()
    await openai_pre_publish_shutdown_and_reload_contracts()
    await openai_bootstrap_failure_cleans_published_process()
    await openai_unpublished_failures_cleanup_exactly_once()
    await openai_actual_bootstrap_persistence_failure_is_terminal()
    await openai_live_and_recovered_cancel_cleanup()
    await remote_ack_and_cancel_races()
    await node_handler_cancel_between_receipt_and_context()
    await node_rehook_restores_nonce_and_cancelling_state()
    await remote_recovery_retries_cancel_before_rehook()
    await remote_primary_crash_waits_for_exact_terminal()
    await remote_public_start_boundary_records_immediate_cancel()
    await node_cancel_reconstructs_accepting_after_restart()
    await node_accepted_send_failure_stays_recoverable()
    await remote_reconnect_recovery_has_one_owner()
    await recovered_attach_pre_admission_shutdown_kills_owned_process()
    await codex_bootstrap_failure_cleans_published_tree()
    await fugu_lifecycle_behaves_with_sakana_config()
    await shared_provider_shutdown_hook_is_single_and_policy_aware()


if __name__ == "__main__":
    asyncio.run(main())
    print("PASS provider lifecycle owner-loop state machine")
