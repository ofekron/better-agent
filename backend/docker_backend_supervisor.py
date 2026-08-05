from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from backend_instance_lock import reserve_primary_backend_instance_lock
from backend_launch_authority import issue_primary_backend_launch
from paths import bc_home
from primary_launcher_lease import PrimaryLauncherLease


def _activate_installation_profile(checkout: Path) -> None:
    import installation_profile
    import provider_setup

    if installation_profile.load().get("status") == "active":
        return
    mode = os.environ.get("BETTER_AGENT_INSTALL_MODE", installation_profile.DESKTOP_UI_ONLY)
    provider = os.environ.get("BETTER_AGENT_PROVIDER", "claude")
    if mode not in installation_profile.MODES:
        raise RuntimeError("unsupported Docker installation mode")
    if provider not in provider_setup.supported_provider_kinds():
        raise RuntimeError("unsupported Docker provider")
    subprocess.run(
        [
            sys.executable,
            str(checkout / "scripts" / "install.py"),
            "--mode",
            mode,
            "--provider",
            provider,
            "--yes",
        ],
        cwd=checkout,
        check=True,
    )


def main() -> int:
    checkout = Path(__file__).resolve().parents[1]
    lease = PrimaryLauncherLease.acquire(bc_home(), checkout=checkout)
    reservation = None
    process: subprocess.Popen[bytes] | None = None

    def forward(signum: int, _frame: object) -> None:
        if process is not None and process.poll() is None:
            process.send_signal(signum)

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        _activate_installation_profile(checkout)
        reservation = reserve_primary_backend_instance_lock(lease)
        env = {
            **os.environ,
            **issue_primary_backend_launch(
                checkout=checkout,
                state_root=bc_home(),
                launcher_lease=lease,
                backend_reservation=reservation,
            ),
        }
        with reservation.child_spawn_kwargs() as inheritance_kwargs:
            process = subprocess.Popen(
                [sys.executable, "-S", str(checkout / "backend" / "backend_bootstrap.py")],
                cwd=checkout / "backend",
                env=env,
                **inheritance_kwargs,
            )
        reservation.await_adoption(process)
        reservation.detach_after_transfer()
        return process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if reservation is not None:
            reservation.release()
        lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
