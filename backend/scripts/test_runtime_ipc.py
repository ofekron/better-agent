"""Runtime IPC transport + daemon/CLI skeleton (plan Phase 2/3).

Locks:
- endpoint and token derive from the isolated home, never fixed /tmp
- authenticated out-of-process roundtrip works (real subprocess client)
- wrong token and missing token fail closed; server survives bad peers
- unknown ops and bad operation kinds map to fail-closed client errors
- daemon owns the writer lock, refuses a second daemon, serves the
  endpoint, and the CLI can observe and stop it
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path

import _test_home

_TEST_HOME = _test_home.isolate(prefix="ba-runtime-ipc-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ask_status_store
import paths
import runtime_ipc
from runtime_ipc import (
    RuntimeIPCAuthError,
    RuntimeIPCClient,
    RuntimeIPCError,
    RuntimeIPCServer,
)

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _current_home() -> str:
    # pytest imports every test module before running any test, and each
    # module's isolate() repoints the process home — always assert
    # against the home that is CURRENT when the test executes.
    return str(paths.ba_home())


def _subprocess_env(home: str) -> dict:
    return {**os.environ, "BETTER_AGENT_HOME": home, "PYTHONPATH": str(_BACKEND_DIR)}


def test_endpoint_and_token_derive_from_home():
    import hashlib

    digest = hashlib.sha256(_current_home().encode("utf-8")).hexdigest()[:16]
    address = runtime_ipc.endpoint_address()
    assert digest in address  # per-home endpoint: different home, different name
    if os.name != "nt":
        # Socket lives in the short per-user dir (AF_UNIX path cap), never
        # at a fixed shared name.
        assert address.startswith(str(runtime_ipc.socket_dir()))
    assert str(runtime_ipc.token_path()).startswith(_current_home())


def test_server_roundtrip_ping_and_operation_status():
    server = RuntimeIPCServer()
    server.start()
    try:
        pong = RuntimeIPCClient().ping()
        assert pong["service"] == "better-agent-runtime"
        assert pong["pid"] == os.getpid()
        if os.name != "nt":
            mode = os.stat(runtime_ipc.token_path()).st_mode & 0o777
            assert mode == 0o600

        ask_status_store.write_status("ask_ipc1", result={"text": "done"})
        out = RuntimeIPCClient().operation_status("ask", "ask_ipc1")
        assert out["found"] is True
        assert out["status"] == "complete"

        try:
            RuntimeIPCClient().operation_status("nope", "x")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown kind")

        try:
            RuntimeIPCClient().call("no_such_op")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown op")
    finally:
        server.stop()


def test_out_of_process_client_roundtrip():
    server = RuntimeIPCServer()
    server.start()
    try:
        ask_status_store.write_status("ask_xproc", result={"text": "ok"})
        home = _current_home()
        script = """
import json
import sys
from runtime_ipc import RuntimeIPCClient
out = RuntimeIPCClient().operation_status("ask", "ask_xproc")
print(json.dumps(out))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=_subprocess_env(home),
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(result.stdout.strip())
        assert out["status"] == "complete"
    finally:
        server.stop()


def test_wrong_token_rejected_and_server_survives():
    server = RuntimeIPCServer()
    server.start()
    try:
        try:
            Client(
                runtime_ipc.endpoint_address(),
                family="AF_PIPE" if os.name == "nt" else "AF_UNIX",
                authkey=b"wrong-token",
            )
        except AuthenticationError:
            pass
        else:
            raise AssertionError("expected AuthenticationError for wrong token")
        # Server must keep serving authenticated clients after a bad peer.
        assert RuntimeIPCClient().ping()["pid"] == os.getpid()
    finally:
        server.stop()


def test_missing_token_fails_closed_before_connecting():
    server = RuntimeIPCServer()
    server.start()
    try:
        token = runtime_ipc.token_path()
        saved = token.read_text(encoding="utf-8")
        token.unlink()
        try:
            RuntimeIPCClient().ping()
        except RuntimeIPCAuthError:
            pass
        else:
            raise AssertionError("expected RuntimeIPCAuthError with no token")
        finally:
            token.write_text(saved, encoding="utf-8")
            if os.name != "nt":
                token.chmod(0o600)
    finally:
        server.stop()


def test_client_without_server_fails_closed():
    try:
        RuntimeIPCClient().ping()
    except RuntimeIPCError:
        pass
    else:
        raise AssertionError("expected RuntimeIPCError with no server")


def test_daemon_lifecycle_writer_lock_and_cli():
    import tempfile

    home = tempfile.mkdtemp(prefix="ba-runtime-daemon-")
    env = _subprocess_env(home)
    daemon = subprocess.Popen(
        [sys.executable, "-m", "runtime_daemon"],
        cwd=str(_BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert daemon.stdout is not None
        ready = json.loads(daemon.stdout.readline())
        assert ready["event"] == "ready"
        assert ready["pid"] == daemon.pid

        status = subprocess.run(
            [sys.executable, "-m", "runtime_cli", "status"],
            cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True,
        )
        assert status.returncode == 0
        assert json.loads(status.stdout)["ipc"]["pid"] == daemon.pid

        # Second daemon on the same home must refuse: one writer per home.
        second = subprocess.run(
            [sys.executable, "-m", "runtime_daemon"],
            cwd=str(_BACKEND_DIR),
            env={**env, "BETTER_AGENT_RUNTIME_LOCK_TIMEOUT": "1"},
            capture_output=True, text=True, timeout=60,
        )
        assert second.returncode == 2

        stop = subprocess.run(
            [sys.executable, "-m", "runtime_cli", "stop"],
            cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True,
        )
        assert stop.returncode == 0
        assert daemon.wait(timeout=15) == 0

        gone = subprocess.run(
            [sys.executable, "-m", "runtime_cli", "status"],
            cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True,
        )
        assert gone.returncode == 1
        assert json.loads(gone.stdout)["ipc"] == {"running": False}
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)


def test_session_snapshot_ops_read_only_roundtrip():
    import runtime_ownership
    import session_store

    payload = {
        "id": "ipc-snap-1",
        "name": "IPC snapshot test",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "messages": [],
        "forks": [],
        "schema_version": session_store.SCHEMA_VERSION,
    }
    with runtime_ownership.runtime_writer():
        session_store.write_session_full(payload)

    server = RuntimeIPCServer()
    server.start()
    try:
        client = RuntimeIPCClient()
        snap = client.session_snapshot("ipc-snap-1")
        assert snap["found"] is True
        assert snap["session"]["id"] == "ipc-snap-1"
        assert snap["events_high_water"] == 0  # no journal rows yet

        from event_ingester import event_ingester

        event_ingester.ingest(
            "ipc-snap-1", "sid-s", "agent_message",
            {"uuid": "u-snap", "text": "row"}, source="test",
        )
        assert client.session_snapshot("ipc-snap-1")["events_high_water"] >= 1

        rows = client.list_sessions()
        assert any(row.get("id") == "ipc-snap-1" for row in rows)

        missing = client.session_snapshot("never-written")
        assert missing == {"found": False, "session": None, "events_high_water": 0}

        try:
            client.session_snapshot("../escape")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unsafe session id")
    finally:
        server.stop()


def test_events_catchup_cursor_roundtrip():
    from event_ingester import event_ingester

    root_id = "ipc-catchup-1"
    event_ingester.ingest(
        root_id, "sid-a", "agent_message",
        {"uuid": "u-1", "text": "one"}, source="test",
    )
    event_ingester.ingest(
        root_id, "sid-a", "agent_message",
        {"uuid": "u-2", "text": "two"}, source="test",
    )

    server = RuntimeIPCServer()
    server.start()
    try:
        client = RuntimeIPCClient()
        first = client.events_catchup(root_id, after_seq=0, limit=1)
        assert len(first["events"]) == 1
        assert first["has_more"] is True
        rest = client.events_catchup(root_id, after_seq=first["next_seq"])
        assert len(rest["events"]) == 1
        assert rest["has_more"] is False
        assert rest["next_seq"] > first["next_seq"]
        done = client.events_catchup(root_id, after_seq=rest["next_seq"])
        assert done["events"] == []

        for bad in (
            {"after_seq": -1},
            {"limit": 0},
            {"limit": 100000},
        ):
            try:
                client.events_catchup(root_id, **bad)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {bad!r}")
    finally:
        server.stop()


def test_submit_prompt_marshals_to_runtime_loop_and_fails_closed():
    import asyncio
    import threading

    import orchestrator
    from startup_tasks import startup_task_registry

    server = RuntimeIPCServer()
    server.start()
    try:
        client = RuntimeIPCClient()

        # No coordinator in this process: fail closed with an error frame.
        saved_default = orchestrator._default_coordinator
        orchestrator._default_coordinator = None
        orchestrator._active_coordinator_var.set(None)
        try:
            client.submit_prompt("sess-1", {"prompt": "hi"})
        except RuntimeIPCError:
            pass
        else:
            raise AssertionError("expected failure without a coordinator")

        class _FakeCoordinator:
            def __init__(self) -> None:
                self.calls = []
                self.loop_at_call = None

            async def submit_prompt_async(self, app_session_id, params):
                self.loop_at_call = asyncio.get_running_loop()
                self.calls.append((app_session_id, params))
                return "queued-123"

        fake = _FakeCoordinator()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        saved_loop = startup_task_registry._loop
        saved_coord = startup_task_registry._coordinator
        orchestrator._default_coordinator = fake
        startup_task_registry.bind(fake, loop)
        try:
            queued_id = client.submit_prompt("sess-1", {"prompt": "hi"})
            assert queued_id == "queued-123"
            assert fake.calls == [("sess-1", {"prompt": "hi"})]
            assert fake.loop_at_call is loop  # ran ON the runtime loop

            try:
                client.call("submit_prompt", app_session_id="sess-1", params="bad")
            except ValueError:
                pass
            else:
                raise AssertionError("expected rejection of non-dict params")
        finally:
            startup_task_registry._loop = saved_loop
            startup_task_registry._coordinator = saved_coord
            orchestrator._default_coordinator = saved_default
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()
    finally:
        server.stop()


def test_shutdown_refused_without_host_opt_in():
    server = RuntimeIPCServer()  # monolith mode: no on_shutdown_request
    server.start()
    try:
        client = RuntimeIPCClient()
        try:
            client.shutdown()
        except ValueError:
            pass
        else:
            raise AssertionError("expected shutdown refusal without opt-in")
        # Endpoint must remain fully alive after the refused attempt.
        assert client.ping()["pid"] == os.getpid()
    finally:
        server.stop()


def test_shutdown_with_opt_in_closes_listener_promptly():
    import threading

    server = RuntimeIPCServer()
    stopped = threading.Event()
    server.on_shutdown_request = stopped.set
    server.start()
    client = RuntimeIPCClient()
    out = client.shutdown()
    assert out["stopping"] is True
    assert stopped.is_set()
    # Listener is closed by the handler itself: no new connection may
    # land in a half-accepted handshake.
    try:
        client.ping()
    except RuntimeIPCError:
        pass
    else:
        raise AssertionError("expected endpoint gone after opted-in shutdown")


def test_submit_prompt_timeout_cancels_never_submitted_work():
    import asyncio
    import threading

    import orchestrator
    from runtime_client import RuntimeUnavailableError, runtime
    from startup_tasks import startup_task_registry

    class _StuckCoordinator:
        def __init__(self) -> None:
            self.cancelled = threading.Event()
            self.blocker = asyncio.Event()

        async def submit_prompt_async(self, app_session_id, params):
            try:
                await self.blocker.wait()  # never set: submission never happens
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return "never"

    fake = _StuckCoordinator()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    saved_default = orchestrator._default_coordinator
    saved_loop = startup_task_registry._loop
    saved_coord = startup_task_registry._coordinator
    orchestrator._default_coordinator = fake
    orchestrator._active_coordinator_var.set(None)
    startup_task_registry.bind(fake, loop)
    try:
        try:
            runtime.submit_prompt_threadsafe(
                "sess-t", {"prompt": "x"}, timeout_seconds=0.1
            )
        except RuntimeUnavailableError:
            pass
        else:
            raise AssertionError("expected timeout error")
        assert fake.cancelled.wait(timeout=10)  # coroutine really cancelled
    finally:
        orchestrator._default_coordinator = saved_default
        startup_task_registry._loop = saved_loop
        startup_task_registry._coordinator = saved_coord
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def test_submit_prompt_timeout_during_sync_tail_reports_real_outcome():
    import asyncio
    import threading

    import orchestrator
    from runtime_client import runtime
    from startup_tasks import startup_task_registry

    class _SyncTailCoordinator:
        """Simulates the real shape: one await, then an uninterruptible
        sync submit section (blocks the loop thread until released)."""

        def __init__(self) -> None:
            self.in_tail = threading.Event()
            self.release = threading.Event()

        async def submit_prompt_async(self, app_session_id, params):
            await asyncio.sleep(0)  # cross the last await point
            self.in_tail.set()
            self.release.wait(timeout=30)  # sync tail: blocks the loop
            return "queued-tail"

    fake = _SyncTailCoordinator()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    saved_default = orchestrator._default_coordinator
    saved_loop = startup_task_registry._loop
    saved_coord = startup_task_registry._coordinator
    orchestrator._default_coordinator = fake
    orchestrator._active_coordinator_var.set(None)
    startup_task_registry.bind(fake, loop)
    try:
        result_box: list = []

        def _submit() -> None:
            result_box.append(
                runtime.submit_prompt_threadsafe(
                    "sess-tail", {"prompt": "x"}, timeout_seconds=0.05
                )
            )

        caller = threading.Thread(target=_submit)
        caller.start()
        assert fake.in_tail.wait(timeout=10)  # timeout fires while in tail
        fake.release.set()
        caller.join(timeout=15)
        assert not caller.is_alive()
        # The submit DID happen; the caller must learn the real id, not a
        # phantom cancellation that would invite a duplicate retry.
        assert result_box == ["queued-tail"]
    finally:
        orchestrator._default_coordinator = saved_default
        startup_task_registry._loop = saved_loop
        startup_task_registry._coordinator = saved_coord
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def test_cold_daemon_lists_preseeded_sessions():
    """A fresh daemon process must serve the sessions already on disk —
    the summary index warms on demand instead of returning []. Locks the
    divergence found by differential-testing the services against dev."""
    import tempfile

    home = tempfile.mkdtemp(prefix="ba-runtime-coldlist-")
    env = _subprocess_env(home)
    seed_script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "backend"))
import runtime_ownership
import session_store
payload = {
    "id": "cold-seeded",
    "name": "Cold list test",
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
    "messages": [],
    "forks": [],
    "schema_version": session_store.SCHEMA_VERSION,
}
with runtime_ownership.runtime_writer():
    session_store.write_session_full(payload, bump_updated_at=False)
"""
    subprocess.run(
        [sys.executable, "-c", seed_script],
        cwd=str(_BACKEND_DIR.parent), env=env, check=True,
    )
    daemon = subprocess.Popen(
        [sys.executable, "-m", "runtime_daemon"],
        cwd=str(_BACKEND_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert daemon.stdout is not None
        assert json.loads(daemon.stdout.readline())["event"] == "ready"
        list_script = """
import json
import sys
from runtime_ipc import RuntimeIPCClient
result = RuntimeIPCClient().call("list_sessions")
print(json.dumps({
    "ids": [row.get("id") for row in result["sessions"]],
    "complete": result["index_complete"],
}))
"""
        out = subprocess.run(
            [sys.executable, "-c", list_script],
            cwd=str(_BACKEND_DIR), env=env,
            capture_output=True, text=True, check=True,
        )
        listed = json.loads(out.stdout.strip())
        assert listed["ids"] == ["cold-seeded"]
        assert listed["complete"] is True
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=10)


def test_scoped_tokens_deny_by_default():
    import runtime_ownership
    import runtime_tokens
    import session_store

    payload = {
        "id": "scoped-sess",
        "name": "Scoped token test",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "messages": [],
        "forks": [],
        "schema_version": session_store.SCHEMA_VERSION,
    }
    with runtime_ownership.runtime_writer():
        session_store.write_session_full(payload, bump_updated_at=False)

    server = RuntimeIPCServer()
    server.start()
    try:
        admin = RuntimeIPCClient()
        assert admin.session_snapshot("scoped-sess")["found"] is True

        agent_token = runtime_tokens.mint(
            "agent", [runtime_tokens.READ], session_id="scoped-sess"
        )
        agent = RuntimeIPCClient(scoped_token=agent_token)

        # Own-session reads work.
        assert agent.session_snapshot("scoped-sess")["found"] is True
        assert agent.events_catchup("scoped-sess")["events"] == []
        # Cross-session and session-less ops are denied.
        for refused in (
            lambda: agent.session_snapshot("some-other-session"),
            lambda: agent.list_sessions(),
            lambda: agent.operation_status("ask", "ask_x"),
            lambda: agent.submit_prompt("scoped-sess", {"prompt": "x"}),  # no write scope
            lambda: agent.shutdown(),  # no control scope
        ):
            try:
                refused()
            except RuntimeIPCAuthError:
                continue
            raise AssertionError("expected scope refusal")

        # Unknown and revoked tokens are refused outright.
        stranger = RuntimeIPCClient(scoped_token="not-a-real-token")
        try:
            stranger.session_snapshot("scoped-sess")
        except RuntimeIPCAuthError:
            pass
        else:
            raise AssertionError("expected refusal for unknown token")
        assert runtime_tokens.revoke(agent_token) is True
        try:
            agent.session_snapshot("scoped-sess")
        except RuntimeIPCAuthError:
            pass
        else:
            raise AssertionError("expected refusal for revoked token")

        # Admin keeps working throughout.
        assert admin.ping()["schema_version"] == runtime_ipc.SCHEMA_VERSION
    finally:
        server.stop()


def test_monolith_wires_ipc_endpoint_start_and_stop():
    source = (_BACKEND_DIR / "main.py").read_text(encoding="utf-8")
    start = source.index("async def on_startup")
    end = source.index("async def on_shutdown")
    startup_source = source[start:end]
    assert "runtime_ipc.RuntimeIPCServer()" in startup_source
    assert "await asyncio.to_thread(server.start)" in startup_source
    shutdown_source = source[end:]
    assert "_runtime_ipc_server.stop()" in shutdown_source


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
