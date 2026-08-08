#!/usr/bin/env python3
"""Primary-launcher-lease bridge for the full-stack Playwright harness.

`backend/app_lifecycle.py::on_startup()` unconditionally calls
`acquire_backend_instance_lock()`, which requires a primary-backend
reservation handed down via 4 env vars from a launcher that first took a
`PrimaryLauncherLease` and reserved the instance lock (see
`backend/backend_instance_lock.py`, `backend/primary_launcher_lease.py`,
`backend/backend_launch_authority.py`). Production launchers
(`backend/docker_backend_supervisor.py`, `desktop/browser_backend_supervisor.py`)
perform this dance; this script is the harness's equivalent, scoped to the
isolated per-test home the Playwright harness (`harness/backend.ts`) spawns.
It mirrors `docker_backend_supervisor.py::main()`'s sequence exactly — lease,
reserve, issue launch authority, spawn with the reservation's inherited fds,
await adoption, detach — just targeting `uvicorn main:app` directly instead of
`backend_bootstrap.py` (no installation-profile activation here: the harness
already runs `scripts/install.py` before this script starts).

The lock genuinely protects single-instance-per-home; since every harness
backend gets its own freshly created isolated home (`mkdtempSync`), this is a
real, correctly scoped reservation for that home, not a stub.

Invoked as: python3 reserve_and_launch_backend.py --host H --port P
  [--debug-logger NAME ...]
Must run with the same env `harness/process-utils.ts::buildBackendEnv`
already builds for the real backend (BETTER_AGENT_HOME et al.), so
`bc_home()` resolves to the isolated per-test home.

`--debug-logger NAME` (repeatable) sets that named logger to DEBUG before
`main:app` is imported, for investigations that need per-flush debug lines
(e.g. `backend.adapters`, `jsonl_tailer`) without touching backend source —
`main.py` sets the ROOT logger to INFO, but a named logger with its own
explicit level takes precedence over inherited root level regardless of
import order, and uvicorn's default log_config doesn't disable existing
loggers. No behavior change when this flag is omitted (falls back to the
plain `-m uvicorn` invocation, byte-identical to before this option existed).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKEND_DIR = _REPO_ROOT / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

from backend_instance_lock import reserve_primary_backend_instance_lock  # noqa: E402
from backend_launch_authority import issue_primary_backend_launch  # noqa: E402
from paths import bc_home  # noqa: E402
from primary_launcher_lease import PrimaryLauncherLease  # noqa: E402

# Generous relative to the ~15s default: lock adoption is the first line of
# on_startup() (before any heavy startup work), but a loaded dev machine
# running several concurrent sessions can still stall process scheduling.
_ADOPTION_TIMEOUT_SECONDS = 45.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--debug-logger", action="append", default=[])
    args = parser.parse_args()

    lease = PrimaryLauncherLease.acquire(bc_home(), checkout=_REPO_ROOT)
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
        reservation = reserve_primary_backend_instance_lock(lease)
        env = {
            **os.environ,
            **issue_primary_backend_launch(
                checkout=_REPO_ROOT,
                state_root=bc_home(),
                launcher_lease=lease,
                backend_reservation=reservation,
            ),
        }
        if args.debug_logger:
            setup = "; ".join(
                f"logging.getLogger({name!r}).setLevel(logging.DEBUG)"
                for name in args.debug_logger
            )
            argv = [
                sys.executable,
                "-c",
                f"import logging; {setup}; import uvicorn; "
                f"uvicorn.run('main:app', host={args.host!r}, port={int(args.port)!r})",
            ]
        else:
            argv = [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                args.host,
                "--port",
                args.port,
            ]
        with reservation.child_spawn_kwargs() as inheritance_kwargs:
            process = subprocess.Popen(
                argv,
                cwd=_BACKEND_DIR,
                env=env,
                **inheritance_kwargs,
            )
        reservation.await_adoption(process, timeout_seconds=_ADOPTION_TIMEOUT_SECONDS)
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
