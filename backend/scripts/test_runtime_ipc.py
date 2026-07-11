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
import runtime_ipc
from runtime_ipc import (
    RuntimeIPCAuthError,
    RuntimeIPCClient,
    RuntimeIPCError,
    RuntimeIPCServer,
)

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _subprocess_env(home: str) -> dict:
    return {**os.environ, "BETTER_AGENT_HOME": home, "PYTHONPATH": str(_BACKEND_DIR)}


def test_endpoint_and_token_derive_from_home():
    address = runtime_ipc.endpoint_address()
    if os.name == "nt":
        assert address.startswith(r"\\.\pipe\better-agent-runtime-")
    else:
        assert address.startswith(_TEST_HOME)
    assert str(runtime_ipc.token_path()).startswith(_TEST_HOME)


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
        script = """
import json
import sys
from runtime_ipc import RuntimeIPCClient
out = RuntimeIPCClient().operation_status("ask", "ask_xproc")
print(json.dumps(out))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=_subprocess_env(_TEST_HOME),
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
        assert json.loads(status.stdout)["pid"] == daemon.pid

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
        assert json.loads(gone.stdout) == {"running": False}
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)


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
