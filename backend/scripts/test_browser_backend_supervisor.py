#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "desktop"))

# desktop/ is excluded from the backend-only test image (.dockerignore); skip
# cleanly when the desktop/private modules are not on path.
import pytest  # noqa: E402

pytest.importorskip("credential_session")

import credential_session  # noqa: E402
import provider_credentials  # noqa: E402
from desktop.browser_backend_supervisor import BrowserBackendSupervisor  # noqa: E402


def _free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return int(port)


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def _port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _fake_checkout(
    root: Path,
    probe_path: Path,
    *,
    read: bool,
    descendant_pid_path: Path | None = None,
) -> Path:
    backend = root / "backend"
    python_path = backend / ".venvs" / "test" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        f"#!/bin/sh\nexec {sys.executable!r} \"$@\"\n",
        encoding="utf-8",
    )
    python_path.chmod(0o700)
    (backend / ".active-venv").write_text(
        ".venvs/test",
        encoding="utf-8",
    )
    (backend / "dependency_plan.py").write_text(
        "import sys\n"
        "raise SystemExit(0 if sys.argv[1:] == ['assert-active-plan'] else 2)\n",
        encoding="utf-8",
    )
    operation = "read" if read else "status"
    descendant_setup = ""
    if descendant_pid_path is not None:
        descendant_setup = (
            "descendant = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            f"open({str(descendant_pid_path)!r}, 'w', encoding='utf-8').write("
            "str(descendant.pid))\n"
        )
    (backend / "main.py").write_text(
        "import json, os, subprocess, sys\n"
        + descendant_setup
        + "fd_before = os.environ.get('BETTER_AGENT_CREDENTIAL_SESSION_FD')\n"
        "import credential_session_client as client\n"
        f"response = client.request({operation!r}, 'provider-browser-test')\n"
        "child = subprocess.run([sys.executable, '-c', "
        "'import credential_session_client as c; print(c.available())'], "
        "capture_output=True, text=True, check=True, env=dict(os.environ))\n"
        f"with open({str(probe_path)!r}, 'a', encoding='utf-8') as probe:\n"
        " probe.write(json.dumps({'fd_before': bool(fd_before), "
        "'fd_after': 'BETTER_AGENT_CREDENTIAL_SESSION_FD' in os.environ, "
        "'child_available': child.stdout.strip(), 'response': response, "
        "'launch_token': os.environ.get('BETTER_AGENT_BACKEND_LAUNCH_TOKEN'), "
        "'launch_generation': os.environ.get('BETTER_AGENT_BACKEND_LAUNCH_GENERATION'), "
        "'active_checkout': os.environ.get('BETTER_AGENT_ACTIVE_CHECKOUT')}) + '\\n')\n"
        "import asyncio\n"
        "async def app(scope, receive, send):\n"
        " if scope['type'] == 'lifespan':\n"
        "  while True:\n"
        "   message = await receive()\n"
        "   if message['type'] == 'lifespan.startup':\n"
        "    await send({'type': 'lifespan.startup.complete'})\n"
        "   elif message['type'] == 'lifespan.shutdown':\n"
        "    await send({'type': 'lifespan.shutdown.complete'})\n"
        "    return\n"
        " if scope['path'] == '/held':\n"
        "  await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        "  await send({'type': 'http.response.body', 'body': b'open\\n', 'more_body': True})\n"
        "  await asyncio.Event().wait()\n"
        " await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        " await send({'type': 'http.response.body', 'body': b'{\"ok\":true}'})\n",
        encoding="utf-8",
    )
    return root


def _pid_running(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    status = result.stdout.strip()
    return result.returncode == 0 and bool(status) and not status.startswith("Z")


def _stop_generation(supervisor: BrowserBackendSupervisor) -> None:
    supervisor.handle({"op": "signal", "signal": "TERM"})
    _wait_until(
        lambda: supervisor.handle({"op": "status"}).get("terminal") is True
    )


def test_fresh_channels_preserve_denial_without_leaking_to_children(temp_root: Path) -> None:
    probe_path = temp_root / "probe.jsonl"
    checkout = _fake_checkout(temp_root / "checkout", probe_path, read=True)
    reads = 0
    real_get = provider_credentials.oskeychain.native_get

    def blocked_get(_service: str, _account: str, *, reason: str | None = None):
        nonlocal reads
        reads += 1
        raise RuntimeError("blocked")

    provider_credentials.oskeychain.native_get = blocked_get
    supervisor = BrowserBackendSupervisor(
        checkout,
        {
            **os.environ,
            "BETTER_AGENT_HOME": str(temp_root / "state"),
            "BETTER_CLAUDE_HOME": str(temp_root / "state"),
            "PYTHONPATH": f"{ROOT / 'backend'}:{ROOT}",
            "PROBE_PATH": str(probe_path),
        },
    )
    try:
        generation_ids: list[str] = []
        for _ in range(2):
            port = _free_port()
            result = supervisor.handle({
                "op": "start",
                "checkout": str(checkout),
                "host": "127.0.0.1",
                "port": port,
            })
            assert isinstance(result.get("pid"), int)
            assert isinstance(result.get("generation_id"), str)
            assert len(result["generation_id"]) == 32
            generation_ids.append(result["generation_id"])
            _wait_until(lambda: probe_path.exists() and len(probe_path.read_text().splitlines()) >= _ + 1)
            _stop_generation(supervisor)
            terminal = supervisor.handle({"op": "status"})
            assert terminal["terminal"] is True
            assert terminal["generation_id"] == result["generation_id"]
            assert terminal["returncode"] == -signal.SIGTERM
        assert len(set(generation_ids)) == 2
        rows = [json.loads(line) for line in probe_path.read_text(encoding="utf-8").splitlines()]
        assert [row["response"]["status"] for row in rows] == ["blocked", "blocked"]
        assert reads == 2
        assert all(row["fd_before"] is True for row in rows)
        assert all(row["fd_after"] is False for row in rows)
        assert all(row["child_available"] == "False" for row in rows)
        assert all(isinstance(row["launch_token"], str) and row["launch_token"] for row in rows)
        assert [row["launch_generation"] for row in rows] == generation_ids
        assert all(
            Path(row["active_checkout"]).resolve() == checkout.resolve()
            for row in rows
        )
        authority = json.loads(
            (temp_root / "state" / "backend_launch_authority.json").read_text(
                encoding="utf-8",
            )
        )
        assert authority["generation"] == generation_ids[-1]
        assert Path(authority["checkout"]).resolve() == checkout.resolve()
        assert Path(authority["state_root"]).resolve() == (temp_root / "state").resolve()
        assert authority["token_sha256"] == hashlib.sha256(
            rows[-1]["launch_token"].encode("utf-8")
        ).hexdigest()
    finally:
        supervisor.shutdown()
        provider_credentials.oskeychain.native_get = real_get


def test_control_server_keeps_handle_out_of_launcher(temp_root: Path) -> None:
    probe_path = temp_root / "control-probe.jsonl"
    launcher_checkout = _fake_checkout(
        temp_root / "control-launcher-checkout",
        temp_root / "unused-launcher-probe.jsonl",
        read=False,
    )
    checkout = _fake_checkout(temp_root / "control-target-checkout", probe_path, read=False)
    state = temp_root / "control-state"
    state.mkdir()
    (state / "active_checkout.json").write_text(
        json.dumps(
            {
                "active": str(checkout),
                "previous": str(launcher_checkout),
                "request_id": "control-target",
                "status": "active",
                "error": "",
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    raw_control_dir = tempfile.mkdtemp(prefix="ba-bs-", dir="/tmp")
    control_dir = Path(raw_control_dir)
    control_dir.chmod(0o700)
    control_path = control_dir / "control.sock"
    env = {
        **os.environ,
        "BETTER_AGENT_HOME": str(state),
        "BETTER_CLAUDE_HOME": str(state),
        "PYTHONPATH": f"{ROOT}:{ROOT / 'backend'}:{ROOT / 'desktop'}",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "desktop.browser_backend_supervisor",
            "--control",
            str(control_path),
            "--launcher-root",
            str(launcher_checkout),
            "--controller-pid",
            str(os.getpid()),
        ],
        env=env,
    )
    try:
        _wait_until(control_path.exists)
        assert "BETTER_AGENT_CREDENTIAL_SESSION_FD" not in os.environ
        port = _free_port()
        started = subprocess.run(
            [
                sys.executable,
                "-m",
                "desktop.browser_backend_control",
                "--control",
                str(control_path),
                "start",
                "--checkout",
                str(checkout),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert int(started.stdout.strip()) > 0
        _wait_until(
            lambda: probe_path.exists()
            and bool(probe_path.read_text(encoding="utf-8").splitlines())
        )
        row = json.loads(probe_path.read_text(encoding="utf-8").splitlines()[0])
        assert {
            key: row[key]
            for key in ("fd_before", "fd_after", "child_available", "response")
        } == {
            "fd_before": True,
            "fd_after": False,
            "child_available": "False",
            "response": {"status": "unknown"},
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "desktop.browser_backend_control",
                "--control",
                str(control_path),
                "shutdown",
            ],
            env=env,
            check=True,
        )
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        try:
            control_path.unlink()
        except FileNotFoundError:
            pass
        control_dir.rmdir()


def test_control_server_rejects_unrelated_callers(temp_root: Path) -> None:
    checkout = _fake_checkout(
        temp_root / "rejected-checkout",
        temp_root / "rejected-probe.jsonl",
        read=False,
    )
    control_dir = Path(tempfile.mkdtemp(prefix="ba-bs-", dir="/tmp"))
    control_path = control_dir / "control.sock"
    env = {
        **os.environ,
        "BETTER_AGENT_HOME": str(temp_root / "rejected-state"),
        "BETTER_CLAUDE_HOME": str(temp_root / "rejected-state"),
        "PYTHONPATH": f"{ROOT}:{ROOT / 'backend'}:{ROOT / 'desktop'}",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "desktop.browser_backend_supervisor",
            "--control",
            str(control_path),
            "--launcher-root",
            str(checkout),
            "--controller-pid",
            str(os.getpid()),
        ],
        env=env,
    )
    try:
        _wait_until(control_path.exists)
        child_code = (
            "import subprocess,sys; "
            "p=subprocess.run([sys.executable,'-m','desktop.browser_backend_control',"
            f"'--control',{str(control_path)!r},'status'],capture_output=True,text=True); "
            "assert p.returncode != 0 and 'direct launcher child' in p.stderr"
        )
        subprocess.run([sys.executable, "-c", child_code], env=env, check=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "desktop.browser_backend_control",
                "--control",
                str(control_path),
                "shutdown",
            ],
            env=env,
            check=True,
        )
        proc.wait(timeout=10)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        try:
            control_path.unlink()
        except FileNotFoundError:
            pass
        control_dir.rmdir()


def test_controller_crash_stops_supervisor_and_backend(temp_root: Path) -> None:
    checkout = _fake_checkout(
        temp_root / "crash-checkout", temp_root / "crash-probe.jsonl", read=False,
    )
    control_dir = Path(tempfile.mkdtemp(prefix="ba-bs-", dir="/tmp"))
    control_path = control_dir / "control.sock"
    pids_path = temp_root / "crash-pids.json"
    env = {
        **os.environ,
        "BETTER_AGENT_HOME": str(temp_root / "crash-state"),
        "BETTER_CLAUDE_HOME": str(temp_root / "crash-state"),
        "PYTHONPATH": f"{ROOT}:{ROOT / 'backend'}:{ROOT / 'desktop'}",
    }
    controller = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "backend/scripts/browser_supervisor_crash_controller.py"),
            str(control_path),
            str(checkout),
            str(_free_port()),
            str(pids_path),
        ],
        env=env,
    )
    manager_pid = backend_pid = None
    try:
        _wait_until(pids_path.exists)
        pids = json.loads(pids_path.read_text(encoding="utf-8"))
        manager_pid = int(pids["manager"])
        backend_pid = int(pids["backend"])
        controller.wait(timeout=10)

        def stopped() -> bool:
            for pid in (manager_pid, backend_pid):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                return False
            return True

        _wait_until(stopped, timeout=15)
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait()
        for pid in (backend_pid, manager_pid):
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            control_path.unlink()
        except FileNotFoundError:
            pass
        control_dir.rmdir()


def test_graceful_drain_timeout_allows_next_generation(temp_root: Path) -> None:
    checkout = _fake_checkout(
        temp_root / "drain-checkout", temp_root / "drain-probe.jsonl", read=False,
    )
    state_root = temp_root / "drain-state"
    supervisor = BrowserBackendSupervisor(
        checkout,
        {
            **os.environ,
            "BETTER_AGENT_HOME": str(state_root),
            "BETTER_CLAUDE_HOME": str(state_root),
            "BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS": "1",
            "PYTHONPATH": f"{ROOT}:{ROOT / 'backend'}:{ROOT / 'desktop'}",
        },
    )
    port = _free_port()
    connection: socket.socket | None = None
    try:
        first = supervisor.handle({
            "op": "start",
            "checkout": str(checkout),
            "host": "127.0.0.1",
            "port": port,
        })
        _wait_until(lambda: _port_accepts(port))
        connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        connection.sendall(
            b"GET /held HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
        )
        assert b"200 OK" in connection.recv(4096)

        started = time.monotonic()
        _stop_generation(supervisor)
        assert time.monotonic() - started < 9

        second = supervisor.handle({
            "op": "start",
            "checkout": str(checkout),
            "host": "127.0.0.1",
            "port": port,
        })
        assert second["generation_id"] != first["generation_id"]
        _stop_generation(supervisor)
    finally:
        if connection is not None:
            connection.close()
        supervisor.shutdown()


def test_forced_backend_death_reaps_generation_descendants(temp_root: Path) -> None:
    if os.name == "nt":
        return
    descendant_pid_path = temp_root / "descendant.pid"
    checkout = _fake_checkout(
        temp_root / "descendant-checkout",
        temp_root / "descendant-probe.jsonl",
        read=False,
        descendant_pid_path=descendant_pid_path,
    )
    supervisor = BrowserBackendSupervisor(
        checkout,
        {
            **os.environ,
            "BETTER_AGENT_HOME": str(temp_root / "descendant-state"),
            "BETTER_CLAUDE_HOME": str(temp_root / "descendant-state"),
            "PYTHONPATH": f"{ROOT}:{ROOT / 'backend'}:{ROOT / 'desktop'}",
        },
    )
    port = _free_port()
    try:
        generation = supervisor.handle({
            "op": "start",
            "checkout": str(checkout),
            "host": "127.0.0.1",
            "port": port,
        })
        _wait_until(descendant_pid_path.exists)
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        assert _pid_running(descendant_pid)

        os.kill(generation["pid"], signal.SIGKILL)
        _wait_until(
            lambda: supervisor.handle({"op": "status"}).get("terminal") is True
        )
        _wait_until(lambda: not _pid_running(descendant_pid))
    finally:
        supervisor.shutdown()


def test_windows_generation_uses_kill_on_close_job() -> None:
    source = (ROOT / "desktop" / "backend_process_owner.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE_NEW_PROCESS_GROUP" in source
    assert "AssignProcessToJobObject" in source
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in source
    assert "TerminateJobObject" in source


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-browser-supervisor-") as raw_temp_root:
        temp_root = Path(raw_temp_root)
        previous_home = os.environ.get("BETTER_AGENT_HOME")
        previous_legacy_home = os.environ.get("BETTER_CLAUDE_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(temp_root / "state")
        os.environ["BETTER_CLAUDE_HOME"] = str(temp_root / "state")
        try:
            test_fresh_channels_preserve_denial_without_leaking_to_children(temp_root)
            test_control_server_keeps_handle_out_of_launcher(temp_root)
            test_control_server_rejects_unrelated_callers(temp_root)
            test_controller_crash_stops_supervisor_and_backend(temp_root)
            test_graceful_drain_timeout_allows_next_generation(temp_root)
            test_forced_backend_death_reaps_generation_descendants(temp_root)
            test_windows_generation_uses_kill_on_close_job()
        finally:
            if previous_home is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous_home
            if previous_legacy_home is None:
                os.environ.pop("BETTER_CLAUDE_HOME", None)
            else:
                os.environ["BETTER_CLAUDE_HOME"] = previous_legacy_home
    print("OK: browser backend credential supervisor")


if __name__ == "__main__":
    main()
