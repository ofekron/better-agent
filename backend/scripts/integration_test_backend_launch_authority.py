#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import backend_launch_authority


CHILD = """
import socket
import sys
import time
import os
sys.path.insert(0, sys.argv[1])
from backend_instance_lock import acquire_backend_instance_lock
acquire_backend_instance_lock()
sock = socket.socket()
sock.bind(("127.0.0.1", int(sys.argv[2])))
sock.listen()
print("READY:" + str("BETTER_AGENT_BACKEND_LAUNCH_TOKEN" in os.environ), flush=True)
time.sleep(30)
"""


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["BETTER_AGENT_HOME"] = str(home)
    env["BETTER_CLAUDE_HOME"] = str(home)
    for key in backend_launch_authority.launch_env_keys():
        env.pop(key, None)
    env.pop("BETTER_AGENT_TEST_MODE", None)
    return env


def _authorized_env(home: Path) -> dict[str, str]:
    env = _base_env(home)
    env.update(
        backend_launch_authority.issue_primary_backend_launch(
            checkout=ROOT,
            state_root=home,
        )
    )
    return env


def _spawn(home: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], int]:
    port = _port()
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(BACKEND), str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, port


def _wait_ready(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY:False"


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _assert_rejected(
    proc: subprocess.Popen[str],
    home: Path,
    *,
    lock_may_exist: bool = False,
) -> None:
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode
    assert "backend launch authority" in stderr.lower(), (stdout, stderr)
    if not lock_may_exist:
        assert not (home / "backend.lock").exists()


def _assert_port_closed(port: int) -> None:
    with socket.socket() as sock:
        sock.settimeout(0.1)
        assert sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-launch-authority-") as raw:
        base = Path(raw)

        unauthorized_home = base / "unauthorized"
        unauthorized_home.mkdir()
        proc, _ = _spawn(unauthorized_home, _base_env(unauthorized_home))
        _assert_rejected(proc, unauthorized_home)

        entry_home = base / "entry"
        entry_home.mkdir()
        entry_env = _base_env(entry_home)
        entry_env["BETTER_CLAUDE_BACKEND_PORT"] = str(_port())
        proc = subprocess.Popen(
            [sys.executable, str(BACKEND / "app_entry.py"), "--serve"],
            env=entry_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _assert_rejected(proc, entry_home)

        uvicorn_home = base / "uvicorn"
        uvicorn_home.mkdir()
        uvicorn_port = _port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(uvicorn_port),
            ],
            cwd=BACKEND,
            env=_base_env(uvicorn_home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while proc.poll() is None:
            _assert_port_closed(uvicorn_port)
            time.sleep(0.01)
        _assert_rejected(proc, uvicorn_home)
        _assert_port_closed(uvicorn_port)

        corrupt_home = base / "corrupt"
        corrupt_home.mkdir()
        corrupt_env = _authorized_env(corrupt_home)
        (corrupt_home / "backend_launch_authority.json").write_text(
            '{"version":1,"unexpected":true}',
            encoding="utf-8",
        )
        proc, _ = _spawn(corrupt_home, corrupt_env)
        _assert_rejected(proc, corrupt_home)

        symlink_home = base / "symlink"
        symlink_home.mkdir()
        symlink_env = _authorized_env(symlink_home)
        authority_path = symlink_home / "backend_launch_authority.json"
        authority_copy = symlink_home / "authority-copy.json"
        authority_path.replace(authority_copy)
        authority_path.symlink_to(authority_copy)
        proc, _ = _spawn(symlink_home, symlink_env)
        _assert_rejected(proc, symlink_home)

        dangling_home = base / "dangling-pointer"
        dangling_home.mkdir()
        dangling_env = _authorized_env(dangling_home)
        (dangling_home / "active_checkout.json").symlink_to(
            dangling_home / "missing-pointer-target.json"
        )
        proc, _ = _spawn(dangling_home, dangling_env)
        _assert_rejected(proc, dangling_home)

        test_home = base / "test"
        test_home.mkdir()
        test_env = _base_env(test_home)
        test_env["BETTER_AGENT_TEST_MODE"] = "1"
        proc, _ = _spawn(test_home, test_env)
        try:
            _wait_ready(proc)
        finally:
            _stop(proc)

        official_home = base / "official"
        official_home.mkdir()
        proc, _ = _spawn(official_home, _authorized_env(official_home))
        try:
            _wait_ready(proc)
        finally:
            _stop(proc)

        mismatch_home = base / "mismatch"
        mismatch_home.mkdir()
        mismatch_env = _authorized_env(mismatch_home)
        mismatch_env["BETTER_AGENT_ACTIVE_CHECKOUT"] = str(base)
        proc, _ = _spawn(mismatch_home, mismatch_env)
        _assert_rejected(proc, mismatch_home)

        for status, pointer_active, accepted in (
            ("active", ROOT, True),
            ("switching", ROOT, True),
            ("reverted", ROOT, True),
            ("failed", base, True),
            ("active", base, False),
            ("unknown", ROOT, False),
        ):
            pointer_home = base / f"pointer-{status}-{accepted}"
            pointer_home.mkdir()
            (pointer_home / "active_checkout.json").write_text(
                json.dumps({"status": status, "active": str(pointer_active)}),
                encoding="utf-8",
            )
            proc, _ = _spawn(pointer_home, _authorized_env(pointer_home))
            if accepted:
                try:
                    _wait_ready(proc)
                finally:
                    _stop(proc)
            else:
                _assert_rejected(proc, pointer_home)

        stale_home = base / "stale"
        stale_home.mkdir()
        holder_env = _authorized_env(stale_home)
        holder, _ = _spawn(stale_home, holder_env)
        _wait_ready(holder)
        waiting_env = _authorized_env(stale_home)
        contender, _ = _spawn(stale_home, waiting_env)
        try:
            time.sleep(0.5)
            assert contender.poll() is None
            current_env = _authorized_env(stale_home)
            _stop(holder)
            _assert_rejected(contender, stale_home, lock_may_exist=True)
            current, _ = _spawn(stale_home, current_env)
            try:
                _wait_ready(current)
            finally:
                _stop(current)
        finally:
            _stop(holder)

        pointer_race_home = base / "pointer-race"
        pointer_race_home.mkdir()
        (pointer_race_home / "active_checkout.json").write_text(
            json.dumps({"status": "active", "active": str(ROOT)}),
            encoding="utf-8",
        )
        holder_env = _authorized_env(pointer_race_home)
        holder, _ = _spawn(pointer_race_home, holder_env)
        _wait_ready(holder)
        waiting_env = _authorized_env(pointer_race_home)
        contender, _ = _spawn(pointer_race_home, waiting_env)
        try:
            time.sleep(0.5)
            assert contender.poll() is None
            (pointer_race_home / "active_checkout.json").write_text(
                json.dumps({"status": "active", "active": str(base)}),
                encoding="utf-8",
            )
            _stop(holder)
            _assert_rejected(contender, pointer_race_home, lock_may_exist=True)
        finally:
            _stop(holder)

    print("backend launch authority integration: ok")


if __name__ == "__main__":
    main()
