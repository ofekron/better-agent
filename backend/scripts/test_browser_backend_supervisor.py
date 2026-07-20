#!/usr/bin/env python3
from __future__ import annotations

import json
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

import credential_session  # noqa: E402
import provider_credentials  # noqa: E402
from desktop.browser_backend_control import request as control_request  # noqa: E402
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


def _fake_checkout(root: Path, probe_path: Path, *, read: bool) -> Path:
    backend = root / "backend"
    python_path = backend / ".venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        f"#!/bin/sh\nexec {str(ROOT / 'backend' / '.venv' / 'bin' / 'python')!r} \"$@\"\n",
        encoding="utf-8",
    )
    python_path.chmod(0o700)
    operation = "read" if read else "status"
    (backend / "main.py").write_text(
        "import json, os, subprocess, sys\n"
        "fd_before = os.environ.get('BETTER_AGENT_CREDENTIAL_SESSION_FD')\n"
        "import credential_session_client as client\n"
        f"response = client.request({operation!r}, 'provider-browser-test')\n"
        "child = subprocess.run([sys.executable, '-c', "
        "'import credential_session_client as c; print(c.available())'], "
        "capture_output=True, text=True, check=True, env=dict(os.environ))\n"
        f"with open({str(probe_path)!r}, 'a', encoding='utf-8') as probe:\n"
        " probe.write(json.dumps({'fd_before': bool(fd_before), "
        "'fd_after': 'BETTER_AGENT_CREDENTIAL_SESSION_FD' in os.environ, "
        "'child_available': child.stdout.strip(), 'response': response}) + '\\n')\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/healthz')\n"
        "def healthz(): return {'ok': True}\n",
        encoding="utf-8",
    )
    return root


def _stop_generation(supervisor: BrowserBackendSupervisor) -> None:
    supervisor.handle({"op": "signal", "signal": "TERM"})
    _wait_until(lambda: supervisor.handle({"op": "status"})["running"] is False)


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
        for _ in range(2):
            port = _free_port()
            result = supervisor.handle({
                "op": "start",
                "checkout": str(checkout),
                "host": "127.0.0.1",
                "port": port,
            })
            assert isinstance(result.get("pid"), int)
            _wait_until(lambda: probe_path.exists() and len(probe_path.read_text().splitlines()) >= _ + 1)
            _stop_generation(supervisor)
        rows = [json.loads(line) for line in probe_path.read_text(encoding="utf-8").splitlines()]
        assert [row["response"]["status"] for row in rows] == ["blocked", "blocked"]
        assert reads == 1
        assert all(row["fd_before"] is True for row in rows)
        assert all(row["fd_after"] is False for row in rows)
        assert all(row["child_available"] == "False" for row in rows)
    finally:
        supervisor.shutdown()
        provider_credentials.oskeychain.native_get = real_get


def test_control_server_keeps_handle_out_of_launcher(temp_root: Path) -> None:
    probe_path = temp_root / "control-probe.jsonl"
    checkout = _fake_checkout(temp_root / "control-checkout", probe_path, read=False)
    state = temp_root / "control-state"
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
            str(checkout),
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
        _wait_until(probe_path.exists)
        row = json.loads(probe_path.read_text(encoding="utf-8").splitlines()[0])
        assert row == {
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
