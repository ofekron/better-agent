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
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_TMP_HOME = Path(_test_home.isolate("ba-mcp-prewarm-"))

from mcp_prewarm import paths as mp_paths  # noqa: E402
from mcp_prewarm import supervisor  # noqa: E402
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
    result = await supervisor.ensure_daemon_ready(
        session_id, ext_id, server_name, real_config, "fp-1", bound_seconds=8.0,
    )
    check(result.ready, "fresh daemon becomes ready")
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
    check(reuse_elapsed < 1.0, "reuse fast path is near-instant")

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

    ready_map = {"_mcp_prewarm_ready": {server_name: "/tmp/does-not-matter.sock"}}
    substituted = extension_store._apply_mcp_prewarm_daemon(real_server_config, server_name, ready_map)
    check(
        substituted is not None
        and substituted["command"] == sys.executable
        and substituted["args"] == ["-m", "mcp_prewarm.stub"],
        "extension_store substitutes the stub command when a ready socket is present",
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


async def test_codex_provider_prewarm_wiring() -> None:
    """Locks the Codex-side mitigation added alongside Claude's: proves
    `CodexProvider._prewarm_extension_mcp_ready` (called from
    `CodexProvider.start_run`, mirroring `ClaudeProvider._spawn_run`)
    actually readies a daemon and returns its socket, that gating
    (bare_config / non-user-facing) short-circuits without touching the
    event loop, and that the resulting `_mcp_prewarm_ready` map changes
    the server config `extension_store` hands to the codex runner --
    the same substitution `with_builtin_mcp_servers` /
    `extension_store.runtime_mcp_server_configs` apply in
    `runner_codex.py`."""
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
        from provider_codex import CodexProvider

        provider = CodexProvider({"id": "codex-test", "name": "Codex test", "kind": "codex"})
        input_payload = {
            "user_facing": True,
            "bare_config": False,
            "app_session_id": "sess-codex-wiring",
        }
        # `start_run` (and thus this method) only ever runs off the
        # backend's event-loop thread via turn_manager._to_turn_dispatch_thread;
        # asyncio.to_thread reproduces that so _run_coro_blocking's
        # no-running-loop assertion holds exactly as it would in production.
        ready_map = await asyncio.to_thread(
            provider._prewarm_extension_mcp_ready, input_payload, "sess-codex-wiring",
        )
        check(
            ready_map.get(server_name) is not None,
            "CodexProvider._prewarm_extension_mcp_ready readies the daemon and returns its socket",
        )

        gated = provider._prewarm_extension_mcp_ready(
            {"bare_config": True, "user_facing": True}, "sess-x",
        )
        check(gated == {}, "bare_config turns skip Codex prewarm entirely")
        gated2 = provider._prewarm_extension_mcp_ready(
            {"bare_config": False, "user_facing": False}, "sess-x",
        )
        check(gated2 == {}, "non-user-facing turns skip Codex prewarm entirely")

        real_server_config = {"command": "irrelevant-cold-spawn", "args": [], "env": {}}
        substituted = extension_store._apply_mcp_prewarm_daemon(
            real_server_config, server_name, {"_mcp_prewarm_ready": ready_map},
        )
        check(
            substituted is not None
            and substituted["command"] == sys.executable
            and substituted["args"] == ["-m", "mcp_prewarm.stub"],
            "a ready Codex-prewarmed daemon substitutes the stub command in the server config codex would receive",
        )
    finally:
        extension_store.runtime_mcp_prewarm_targets = original


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
    test_stub_forwarding()
    await test_readiness_gate_fail_closed()
    await test_concurrent_latency_win()
    await test_codex_provider_prewarm_wiring()


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
