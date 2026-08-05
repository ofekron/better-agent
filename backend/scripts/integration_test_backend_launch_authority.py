#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from backend_instance_lock import reserve_primary_backend_instance_lock
from backend_launch_authority import issue_primary_backend_launch, launch_env_keys
from primary_launcher_lease import PrimaryLauncherLease


CHILD = """
import os, sys
sys.path.insert(0, sys.argv[1])
from backend_instance_lock import adopt_primary_backend_instance_lock
adopt_primary_backend_instance_lock()
assert "BETTER_AGENT_BACKEND_LAUNCH_TOKEN" not in os.environ
assert "BETTER_AGENT_BACKEND_LAUNCH_GENERATION" not in os.environ
assert "BETTER_AGENT_BACKEND_RESERVATION_ID" not in os.environ
print("READY", flush=True)
sys.stdin.read(1)
"""


def _base_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["BETTER_AGENT_HOME"] = str(home)
    env["BETTER_CLAUDE_HOME"] = str(home)
    env.pop("BETTER_AGENT_TEST_MODE", None)
    for key in launch_env_keys():
        env.pop(key, None)
    return env


def _assert_unreserved_entrypoint_rejected(home: Path) -> None:
    env = _base_env(home)
    env["BETTER_CLAUDE_BACKEND_PORT"] = "18791"
    proc = subprocess.run(
        [sys.executable, str(BACKEND / "app_entry.py"), "--serve"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode != 0
    assert "reservation environment is missing" in proc.stderr


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-launch-authority-") as raw:
        home = Path(raw)
        _assert_unreserved_entrypoint_rejected(home)

        lease = PrimaryLauncherLease.acquire(home, checkout=ROOT)
        reservation = reserve_primary_backend_instance_lock(lease)
        env = {
            **_base_env(home),
            **issue_primary_backend_launch(
                checkout=ROOT,
                state_root=home,
                launcher_lease=lease,
                backend_reservation=reservation,
            ),
        }
        child = None
        try:
            with reservation.child_spawn_kwargs() as popen_kwargs:
                child = subprocess.Popen(
                    [sys.executable, "-c", CHILD, str(BACKEND)],
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **popen_kwargs,
                )
            reservation.await_adoption(child)
            reservation.detach_after_transfer()
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "READY"
            assert not (home / "backend_launch_authority.json").exists()
            try:
                PrimaryLauncherLease.acquire(home, checkout=ROOT)
            except RuntimeError:
                pass
            else:
                raise AssertionError("second launcher acquired the same state home")
        finally:
            if child is not None and child.poll() is None:
                assert child.stdin is not None
                child.stdin.write("x")
                child.stdin.flush()
                child.wait(timeout=10)
            reservation.release()
            lease.release()

        next_lease = PrimaryLauncherLease.acquire(home, checkout=ROOT)
        next_reservation = reserve_primary_backend_instance_lock(next_lease)
        next_reservation.release()
        next_lease.release()


def test_backend_launch_authority_integration() -> None:
    main()


if __name__ == "__main__":
    main()
