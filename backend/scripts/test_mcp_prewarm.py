#!/usr/bin/env python3
"""Locks the mcp_prewarm subsystem: daemon supervisor lifecycle, the
stub's dumb-byte-pipe forwarding, the fail-closed readiness gate (both
in isolation and through the real `extension_store` integration
point), and the concurrent-readiness latency property the whole
subsystem exists to prove.

Reverting `supervisor.ensure_daemon_ready`, `daemon_process.py`,
`stub.py`, or `extension_store._apply_mcp_prewarm_daemon` makes one or
more of these fail.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_TMP_HOME = Path(_test_home.isolate("ba-mcp-prewarm-"))

from mcp_prewarm import paths as mp_paths  # noqa: E402
from mcp_prewarm import supervisor  # noqa: E402
from mcp_prewarm import tcp_transport  # noqa: E402
import extension_store  # noqa: E402

FAILURES: list[str] = []
_FIXTURE_DIR = Path(__file__).resolve().parent


def check(condition: bool, message: str) -> None:
    print(f"  {'✓' if condition else '✗'} {message}")
    if not condition:
        FAILURES.append(message)


def _fixture_real_config(*, extra_env: dict[str, str] | None = None) -> dict:
    env = {"PYTHONPATH": str(_FIXTURE_DIR)}
    if extra_env:
        env.update(extra_env)
    return {
        "command": sys.executable,
        "args": ["-m", "_fixture_mcp_prewarm_module"],
        "env": env,
    }


async def test_supervisor_lifecycle() -> None:
    session_id = "sess-lifecycle"
    ext_id = "fixture-ext"
    server_name = "fixture-server"
    real_config = _fixture_real_config()

    # 1) fresh start -> ready
    cold_started = time.monotonic()
    result = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-1", bound_seconds=8.0,
    )
    cold_elapsed = time.monotonic() - cold_started
    check(result.ready, f"fresh daemon becomes ready in {cold_elapsed * 1000:.1f}ms")
    state = supervisor._read_state(mp_paths.state_path(session_id, ext_id, server_name))
    pid_a = state["pid"] if state else None
    check(bool(pid_a) and supervisor._pid_alive(pid_a), "daemon process is alive after start")

    # 2) reuse -> same pid, no respawn
    reuse_started = time.monotonic()
    result2 = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-1", bound_seconds=8.0,
    )
    reuse_elapsed = time.monotonic() - reuse_started
    state2 = supervisor._read_state(mp_paths.state_path(session_id, ext_id, server_name))
    check(result2.ready, "reused daemon reports ready")
    check(state2 is not None and state2.get("pid") == pid_a, "reuse keeps the same pid (no respawn)")
    check(reuse_elapsed < 1.0, f"reuse fast path is {reuse_elapsed * 1000:.1f}ms")

    # 3) fingerprint change -> old process killed, new one spawned
    result3 = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-2", bound_seconds=8.0,
    )
    check(result3.ready, "daemon becomes ready again after fingerprint change")
    state3 = supervisor._read_state(mp_paths.state_path(session_id, ext_id, server_name))
    pid_b = state3["pid"] if state3 else None
    check(pid_b != pid_a, "fingerprint change spawns a new pid")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and supervisor._pid_alive(pid_a):
        await asyncio.sleep(0.05)
    check(not supervisor._pid_alive(pid_a), "old daemon process was actually terminated")

    # 4) crash-restart: kill -9 the live daemon out from under the supervisor
    os.kill(pid_b, signal.SIGKILL)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and supervisor._pid_alive(pid_b):
        await asyncio.sleep(0.05)
    check(not supervisor._pid_alive(pid_b), "kill -9 actually killed the daemon")
    result4 = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-2", bound_seconds=8.0,
    )
    state4 = supervisor._read_state(mp_paths.state_path(session_id, ext_id, server_name))
    pid_c = state4["pid"] if state4 else None
    check(result4.ready, "supervisor detects deadness and respawns")
    check(pid_c is not None and pid_c != pid_b, "respawned daemon has a fresh pid")

    # 5) idle reap: tiny idle timeout, zero connections -> self-exits
    idle_session = "sess-idle-reap"
    result5 = await supervisor.ensure_daemon_ready(
        idle_session, ext_id, server_name, real_config, "fp-idle",
        bound_seconds=8.0, idle_timeout_seconds=0.3,
    )
    check(result5.ready, "idle-reap daemon becomes ready")
    idle_state = supervisor._read_state(mp_paths.state_path(idle_session, ext_id, server_name))
    idle_pid = idle_state["pid"] if idle_state else None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and supervisor._pid_alive(idle_pid):
        await asyncio.sleep(0.05)
    check(not supervisor._pid_alive(idle_pid), "daemon self-exits after idle timeout with zero connections")


async def test_python_file_low_level_server_factory() -> None:
    fixture = _FIXTURE_DIR / "_fixture_mcp_prewarm_module.py"
    real_config = {
        "command": sys.executable,
        "args": [
            "-m",
            "better_agent_sdk.script_entrypoint",
            str(_FIXTURE_DIR),
            str(fixture),
        ],
        "env": {
            "PYTHONPATH": os.pathsep.join((str(_FIXTURE_DIR), str(ROOT.parent / "sdk"))),
            "MCP_PREWARM_FIXTURE_LOW_LEVEL": "1",
        },
    }
    result = await supervisor.ensure_daemon_ready(
        "sess-low-level-script",
        "fixture-ext-low-level",
        "fixture-server-low-level",
        real_config,
        "fp-low-level-script",
        bound_seconds=8.0,
    )
    log_path = mp_paths.log_path(
        "sess-low-level-script",
        "fixture-ext-low-level",
        "fixture-server-low-level",
    )
    detail = log_path.read_text(encoding="utf-8") if log_path.is_file() else "no daemon log"
    assert result.ready, (
        "python-file build_server() may return a low-level MCP Server\n" + detail
    )


def test_manifest_drives_prewarm_independently_of_user_facing() -> None:
    record = {
        "manifest": {
            "id": "fixture-extension",
            "entrypoints": {"mcp": [{
                "name": "fixture-server",
                "python": "mcp/server.py",
                "execution": "warm_pool",
                "user_facing": False,
                "requires_backend_auth": False,
            }]},
        },
        "source": {"install_path": str(_FIXTURE_DIR)},
    }
    real_config = _fixture_real_config()
    with (
        patch.object(extension_store, "_active_records", return_value=[record]),
        patch.object(extension_store, "_record_runtime_ready", return_value=True),
        patch.object(extension_store, "runtime_package_root_for_record", return_value=_FIXTURE_DIR),
        patch.object(extension_store, "is_mcp_server_enabled", return_value=True),
        patch.object(extension_store, "_runtime_mcp_server_config_for_item", return_value=real_config),
    ):
        targets = extension_store.runtime_mcp_prewarm_targets({
            "app_session_id": "sess-non-user-facing",
            "user_facing": False,
            "bare_config": False,
        })
    assert [target["server_name"] for target in targets] == ["fixture-server"], (
        "warm_pool selection is driven by execution mode, not user_facing"
    )


_INITIALIZE_REQUEST = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05",'
    b'"capabilities":{},"clientInfo":{"name":"test","version":"0"}}}\n'
)
_INITIALIZED_NOTIFICATION = (
    b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
)
_TOOLS_LIST_REQUEST = b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'


async def test_extension_paths_module_cannot_kill_idle_reaper() -> None:
    session_id = "sess-paths-collision"
    ext_id = "fixture-ext-paths-collision"
    server_name = "fixture-server-paths-collision"
    session_file = _TMP_HOME / "sessions" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}", encoding="utf-8")
    real_config = _fixture_real_config(
        extra_env={"MCP_PREWARM_FIXTURE_PATHS_COLLISION": "1"},
    )
    daemon_pid = None
    proc = None
    try:
        result = await supervisor.ensure_daemon_ready(
            session_id,
            ext_id,
            server_name,
            real_config,
            "fp-paths-collision",
            bound_seconds=8.0,
            idle_timeout_seconds=5.0,
        )
        daemon_log = mp_paths.log_path(session_id, ext_id, server_name)
        detail = daemon_log.read_text(encoding="utf-8") if daemon_log.is_file() else ""
        assert result.ready, detail
        state = supervisor._read_state(mp_paths.state_path(session_id, ext_id, server_name))
        assert state is not None
        daemon_pid = state["pid"]
        spawn_config = supervisor._read_state(
            mp_paths.spawn_config_path(session_id, ext_id, server_name),
        )
        assert spawn_config is not None
        assert Path(spawn_config["sessions_dir"]) == session_file.parent

        if result.transport == "tcp":
            assert result.host and result.port and result.connect_secret
            stub_env = {
                "BETTER_AGENT_MCP_DAEMON_ADDR": f"{result.host}:{result.port}",
                "BETTER_AGENT_MCP_DAEMON_CONNECT_SECRET": result.connect_secret,
            }
        else:
            assert result.socket_path
            stub_env = {"BETTER_AGENT_MCP_DAEMON_SOCKET": result.socket_path}
        proc = _run_stub(stub_env)
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(_INITIALIZE_REQUEST)
        proc.stdin.flush()
        initialize_response = await asyncio.wait_for(
            asyncio.to_thread(proc.stdout.readline),
            timeout=5.0,
        )
        detail = daemon_log.read_text(encoding="utf-8") if daemon_log.is_file() else ""
        assert b'"id":1' in initialize_response and b'"result"' in initialize_response, detail
        proc.stdin.write(_INITIALIZED_NOTIFICATION + _TOOLS_LIST_REQUEST)
        proc.stdin.flush()
        tools_response = await asyncio.wait_for(
            asyncio.to_thread(proc.stdout.readline),
            timeout=5.0,
        )
        detail = daemon_log.read_text(encoding="utf-8") if daemon_log.is_file() else ""
        assert b'"id":2' in tools_response and b'"tools"' in tools_response, detail

        await asyncio.sleep(0.5)
        surviving_state = supervisor._read_state(
            mp_paths.state_path(session_id, ext_id, server_name),
        )
        assert surviving_state is not None and surviving_state["pid"] == daemon_pid
        assert supervisor._pid_alive(daemon_pid)
        if result.transport == "tcp":
            assert surviving_state.get("host") == result.host
            assert surviving_state.get("port") == result.port
            assert surviving_state.get("connect_secret") == result.connect_secret
        else:
            assert result.socket_path and Path(result.socket_path).exists()

        session_file.unlink()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and supervisor._pid_alive(daemon_pid):
            await asyncio.sleep(0.05)
        assert not supervisor._pid_alive(daemon_pid)
    finally:
        if proc is not None and proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
        state = supervisor._read_state(mp_paths.state_path(session_id, ext_id, server_name))
        cleanup_pid = daemon_pid or (state.get("pid") if state else None)
        if cleanup_pid and supervisor._pid_alive(cleanup_pid):
            supervisor._terminate(cleanup_pid)
        session_file.unlink(missing_ok=True)


async def test_tcp_daemon_secret_gate() -> None:
    """Forces the Windows transport directly via `transport="tcp"` so the
    real listener/secret-gate/proxy-loop code path is exercised for real
    on this macOS/Linux test host, without needing `sys.platform` to
    actually be "win32". Proves both halves of the security boundary:
    a correct secret gets real MCP proxying, a wrong or missing secret
    gets the connection closed with zero bytes proxied."""
    session_id = "sess-tcp-gate"
    ext_id = "fixture-ext-tcp"
    server_name = "fixture-server-tcp"
    real_config = _fixture_real_config()

    result = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-tcp-1", bound_seconds=8.0, transport="tcp",
    )
    check(result.ready, "tcp-transport daemon becomes ready")
    check(result.transport == "tcp", "ready result reports tcp transport")
    check(
        bool(result.host) and bool(result.port) and bool(result.connect_secret),
        "tcp ready result carries host/port/connect_secret",
    )

    good_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    good_sock.settimeout(5)
    try:
        good_sock.connect((result.host, result.port))
        good_sock.sendall(tcp_transport.encode_secret_frame(result.connect_secret))
        good_sock.sendall(_INITIALIZE_REQUEST)
        response = good_sock.recv(65536)
    finally:
        good_sock.close()
    check(
        b'"id":1' in response and b'"result"' in response,
        "correct secret gets real MCP proxying (initialize responds)",
    )

    bad_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bad_sock.settimeout(5)
    try:
        bad_sock.connect((result.host, result.port))
        bad_sock.sendall(tcp_transport.encode_secret_frame("wrong-secret-value"))
        bad_sock.sendall(_INITIALIZE_REQUEST)
        bad_response = bad_sock.recv(65536)
    finally:
        bad_sock.close()
    check(bad_response == b"", "wrong secret closes the connection without proxying any bytes")

    garbage_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    garbage_sock.settimeout(5)
    try:
        garbage_sock.connect((result.host, result.port))
        garbage_sock.sendall(b"not a length-prefixed secret frame at all")
        garbage_response = garbage_sock.recv(65536)
    finally:
        garbage_sock.close()
    check(garbage_response == b"", "malformed/missing secret frame closes the connection without proxying")


def _run_stub(env_overrides: dict[str, str]) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT), **env_overrides}
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_prewarm.stub"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(ROOT),
    )


def test_stub_forwarding() -> None:
    import socketserver
    import threading

    socket_dir = _TMP_HOME / "stub-echo"
    socket_dir.mkdir(parents=True, exist_ok=True)
    socket_path = socket_dir / "echo.sock"
    if socket_path.exists():
        socket_path.unlink()

    class _Echo(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            while True:
                data = self.request.recv(65536)
                if not data:
                    break
                self.request.sendall(data)

    server = socketserver.UnixStreamServer(str(socket_path), _Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = _run_stub({"BETTER_CLAUDE_MCP_DAEMON_SOCKET": str(socket_path)})
        try:
            payload = b"hello mcp prewarm stub\n"
            proc.stdin.write(payload)
            proc.stdin.flush()
            echoed = proc.stdout.read(len(payload))
            check(echoed == payload, "stub forwards stdin bytes to socket and back to stdout unchanged")
        finally:
            proc.stdin.close()
            returncode = proc.wait(timeout=5)
            check(returncode == 0, "stub exits 0 on clean EOF")
    finally:
        server.shutdown()
        server.server_close()

    # error path: no daemon listening -> stub exits non-zero immediately
    bad_proc = _run_stub({"BETTER_CLAUDE_MCP_DAEMON_SOCKET": str(socket_dir / "no-such.sock")})
    bad_proc.stdin.close()
    bad_returncode = bad_proc.wait(timeout=5)
    check(bad_returncode != 0, "stub exits non-zero when it cannot connect to the daemon socket")


def test_stub_tcp_forwarding() -> None:
    """Windows code path for the stub: connects over TCP, sends the
    secret frame first, then behaves as the exact same transparent pipe
    proven above for the unix path."""
    import socketserver
    import threading

    connect_secret = "test-connect-secret-value"
    received_frames: list[bytes] = []

    class _SecretGatedEcho(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            received = _read_length_prefixed_frame_sync(self.request)
            received_frames.append(received)
            if not tcp_transport.secrets_match(connect_secret, received):
                return
            while True:
                data = self.request.recv(65536)
                if not data:
                    break
                self.request.sendall(data)

    def _read_length_prefixed_frame_sync(sock: socket.socket) -> bytes:
        # Deliberately an independent, from-scratch re-implementation of
        # the 4-byte-length-prefix framing (not a call into
        # `tcp_transport`) -- this is the test acting as a foreign/hostile
        # server peer verifying what the stub actually puts on the wire,
        # not re-checking the implementation against itself.
        import struct

        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                return b""
            header += chunk
        (length,) = struct.unpack("!I", header)
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                break
            payload += chunk
        return payload

    server = socketserver.TCPServer(("127.0.0.1", 0), _SecretGatedEcho)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = _run_stub({
            "BETTER_CLAUDE_MCP_DAEMON_ADDR": f"{host}:{port}",
            "BETTER_CLAUDE_MCP_DAEMON_CONNECT_SECRET": connect_secret,
        })
        try:
            payload = b"hello mcp prewarm tcp stub\n"
            proc.stdin.write(payload)
            proc.stdin.flush()
            echoed = proc.stdout.read(len(payload))
            check(echoed == payload, "tcp stub forwards stdin bytes to the daemon and back to stdout unchanged")
        finally:
            proc.stdin.close()
            returncode = proc.wait(timeout=5)
            check(returncode == 0, "tcp stub exits 0 on clean EOF")
    finally:
        server.shutdown()
        server.server_close()
    check(
        len(received_frames) == 1 and received_frames[0] == connect_secret.encode("utf-8"),
        "tcp stub sends the connect secret as the first framed message before any payload bytes",
    )

    # error path: nothing listening at the given address -> stub exits non-zero
    bad_proc = _run_stub({
        "BETTER_CLAUDE_MCP_DAEMON_ADDR": "127.0.0.1:1",
        "BETTER_CLAUDE_MCP_DAEMON_CONNECT_SECRET": connect_secret,
    })
    bad_proc.stdin.close()
    bad_returncode = bad_proc.wait(timeout=5)
    check(bad_returncode != 0, "tcp stub exits non-zero when it cannot connect to the daemon address")

    # error path: address set but no secret -> stub refuses to connect at all
    nosecret_proc = _run_stub({"BETTER_CLAUDE_MCP_DAEMON_ADDR": f"{host}:{port}"})
    nosecret_proc.stdin.close()
    nosecret_returncode = nosecret_proc.wait(timeout=5)
    check(nosecret_returncode != 0, "tcp stub exits non-zero when no connect secret is available in env")


async def test_readiness_gate_fail_closed() -> None:
    session_id = "sess-fail-closed"
    ext_id = "fixture-ext-slow"
    server_name = "fixture-slow-server"
    real_config = _fixture_real_config(extra_env={"MCP_PREWARM_FIXTURE_DELAY_SECONDS": "5"})

    started = time.monotonic()
    result = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-slow", bound_seconds=1.0,
    )
    elapsed = time.monotonic() - started
    check(not result.ready, "daemon that starts slower than bound_seconds reports not-ready")
    check(elapsed < 3.0, "fail-closed gate returns near bound_seconds, not indefinitely")

    # extension_store integration: prewarm was attempted (key present) and
    # failed for this server -> config omitted, never falls back to a real
    # cold-spawn command for the turn.
    real_server_config = {"command": "irrelevant", "args": [], "env": {}}
    omitted = extension_store._apply_mcp_prewarm_daemon(
        real_server_config, server_name, {"_mcp_prewarm_ready": {server_name: None}},
    )
    check(omitted is None, "extension_store omits the server when prewarm was attempted and failed")

    not_attempted = extension_store._apply_mcp_prewarm_daemon(
        real_server_config, server_name, {},
    )
    check(
        not_attempted == real_server_config,
        "extension_store falls back to real cold-spawn when prewarm was never attempted for this call",
    )

    missing_server = extension_store._apply_mcp_prewarm_daemon(
        real_server_config, server_name, {"_mcp_prewarm_ready": {}},
    )
    check(
        missing_server == real_server_config,
        "extension_store cold-spawns a server absent from the prewarm result map",
    )

    ready_map = {"_mcp_prewarm_ready": {server_name: "/tmp/does-not-matter.sock"}}
    substituted = extension_store._apply_mcp_prewarm_daemon(real_server_config, server_name, ready_map)
    check(
        substituted is not None
        and substituted["command"] == sys.executable
        and substituted["args"] == ["-m", "mcp_prewarm.stub"],
        "extension_store substitutes the stub command when a ready socket is present",
    )

    # Windows/tcp shape: `DaemonReadyResult.ready_map_value()` produces a
    # dict instead of a bare path -- extension_store detects it by type
    # and wires the addr/secret env vars instead of the socket-path env var.
    tcp_ready_result = supervisor.DaemonReadyResult(
        ready=True, transport="tcp", host="127.0.0.1", port=54321, connect_secret="s3cr3t",
    )
    tcp_ready_map = {"_mcp_prewarm_ready": {server_name: tcp_ready_result.ready_map_value()}}
    tcp_substituted = extension_store._apply_mcp_prewarm_daemon(real_server_config, server_name, tcp_ready_map)
    check(
        tcp_substituted is not None
        and tcp_substituted["command"] == sys.executable
        and tcp_substituted["args"] == ["-m", "mcp_prewarm.stub"],
        "extension_store substitutes the stub command for a ready tcp-transport daemon",
    )
    tcp_env = tcp_substituted["env"] if tcp_substituted else {}
    check(
        tcp_env.get("BETTER_CLAUDE_MCP_DAEMON_ADDR") == "127.0.0.1:54321"
        and tcp_env.get("BETTER_AGENT_MCP_DAEMON_ADDR") == "127.0.0.1:54321"
        and tcp_env.get("BETTER_CLAUDE_MCP_DAEMON_CONNECT_SECRET") == "s3cr3t"
        and tcp_env.get("BETTER_AGENT_MCP_DAEMON_CONNECT_SECRET") == "s3cr3t"
        and "BETTER_CLAUDE_MCP_DAEMON_SOCKET" not in tcp_env,
        "tcp-transport substitution wires addr+secret env vars (both dual_env_many aliases), not a socket path",
    )


async def test_concurrent_latency_win() -> None:
    """Nine sessions pre-warm truly concurrently. This models the CLI's
    ~2-6s MCP snapshot race: if daemons could only be readied serially
    (the property a naive/broken implementation would exhibit), 9
    fixture spawns at realistic interpreter-import cost would blow well
    past that window. Asserting they land inside a bound comfortably
    under the CLI's window is the concrete latency-win proof."""
    real_config = _fixture_real_config()
    session_ids = [f"sess-latency-{i}" for i in range(9)]

    async def _one(session_id: str):
        return await supervisor.ensure_daemon_ready(
            session_id, "fixture-ext", "fixture-server", real_config, "fp-latency",
            bound_seconds=8.0,
        )

    started = time.monotonic()
    results = await asyncio.gather(*(_one(sid) for sid in session_ids))
    elapsed = time.monotonic() - started
    check(all(r.ready for r in results), "all 9 concurrent daemons report ready")
    # 6.5s: comfortably under Codex's 10s startup_timeout_sec (the wider
    # of the two CLI windows this fixes) with margin for sandbox/CI load
    # noise -- observed 0.7-5.3s across repeated runs on a loaded box.
    check(elapsed < 6.5, f"9 concurrent daemons ready in {elapsed:.2f}s (< 6.5s bound)")


async def test_shared_provider_prewarm_wiring() -> None:
    server_name = "fixture-server-codex"
    ext_id = "fixture-ext-codex"
    real_config = _fixture_real_config()
    target = {
        "extension_id": ext_id,
        "server_name": server_name,
        "real_config": real_config,
        "extension_record": {"manifest": {"id": ext_id}},
    }
    original = extension_store.runtime_mcp_prewarm_targets
    extension_store.runtime_mcp_prewarm_targets = lambda inputs: [target]
    try:
        from mcp_prewarm.preparation import prepare_runtime_mcp_prewarm

        input_payload = {
            "user_facing": False,
            "bare_config": False,
            "app_session_id": "sess-codex-wiring",
        }
        prepared = await asyncio.to_thread(
            prepare_runtime_mcp_prewarm,
            input_payload,
            "sess-codex-wiring",
            bound_seconds=8.0,
        )
        assert prepared.ready_map.get(server_name) is not None
        assert prepared.status == {
            server_name: {"status": "ready", "error": None},
        }
        for provider_file in (
            "provider_claude.py",
            "provider_claude_better_agent_runner.py",
            "provider_codex.py",
            "provider_agy.py",
        ):
            source = (ROOT / provider_file).read_text(encoding="utf-8")
            assert "prepare_runtime_mcp_prewarm(" in source, (
                f"{provider_file} bypasses the shared warm_pool preparation"
            )

        real_server_config = {"command": "irrelevant-cold-spawn", "args": [], "env": {}}
        substituted = extension_store._apply_mcp_prewarm_daemon(
            real_server_config,
            server_name,
            {"_mcp_prewarm_ready": prepared.ready_map},
        )
        assert substituted is not None
        assert substituted["command"] == sys.executable
        assert substituted["args"] == ["-m", "mcp_prewarm.stub"]
    finally:
        extension_store.runtime_mcp_prewarm_targets = original


def test_agent_board_declares_warm_pool_without_user_facing() -> None:
    manifest_path = (
        ROOT.parent
        / "better-agent-private"
        / "extensions"
        / "agent-board"
        / "better-agent-extension.json"
    )
    if not manifest_path.is_file():
        return
    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = manifest["entrypoints"]["mcp"][0]
    assert item["execution"] == "warm_pool"
    assert item["user_facing"] is False
    adapter = manifest_path.parent / item["python"]
    assert "def build_server(" in adapter.read_text(encoding="utf-8")


def _cleanup_all_sessions() -> None:
    for session_dir in mp_paths.root().glob("*"):
        for ext_dir in session_dir.glob("*"):
            for server_dir in ext_dir.glob("*"):
                state = supervisor._read_state(server_dir / "state.json")
                pid = state.get("pid") if state else None
                if pid and supervisor._pid_alive(pid):
                    supervisor._terminate(pid)


async def main_async() -> None:
    await test_supervisor_lifecycle()
    await test_python_file_low_level_server_factory()
    await test_extension_paths_module_cannot_kill_idle_reaper()
    test_manifest_drives_prewarm_independently_of_user_facing()
    test_stub_forwarding()
    await test_tcp_daemon_secret_gate()
    test_stub_tcp_forwarding()
    await test_readiness_gate_fail_closed()
    await test_concurrent_latency_win()
    await test_shared_provider_prewarm_wiring()
    test_agent_board_declares_warm_pool_without_user_facing()


def main() -> int:
    try:
        asyncio.run(main_async())
    finally:
        _cleanup_all_sessions()
        import shutil
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} assertion(s)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
