from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import signal
import stat
import sys
import threading
from pathlib import Path

from paths import make_private_file, require_private_directory
from primary_launcher_lease import PrimaryLauncherLease


def _arm_controller_death(controller_pid: int) -> None:
    if os.getppid() != controller_pid:
        raise RuntimeError("launcher lease guard is not owned by its controller")
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:
            raise OSError(ctypes.get_errno(), "failed to arm parent-death signal")
        if os.getppid() != controller_pid:
            os.kill(os.getpid(), signal.SIGTERM)
        return
    if sys.platform == "darwin":
        queue = select.kqueue()
        event = select.kevent(
            controller_pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
            fflags=select.KQ_NOTE_EXIT,
        )
        queue.control([event], 0)
        if os.getppid() != controller_pid:
            queue.close()
            os.kill(os.getpid(), signal.SIGTERM)
            return

        def wait_for_exit() -> None:
            try:
                queue.control(None, 1)
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                queue.close()

        threading.Thread(
            target=wait_for_exit,
            name="launcher-controller-watch",
            daemon=True,
        ).start()
        return
    raise RuntimeError("launcher lease guard controller monitoring is unavailable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--controller-pid", type=int, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--gate-fifo", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _validate(args: argparse.Namespace) -> tuple[Path, Path, list[str]]:
    if os.name == "nt":
        raise RuntimeError("launcher lease guard requires POSIX descriptor handoff")
    if os.getppid() != args.controller_pid:
        raise RuntimeError("launcher lease guard is not owned by its controller")
    checkout = args.checkout.expanduser().resolve(strict=True)
    state_root = args.state_root.expanduser().resolve(strict=True)
    control_root = args.ready_file.parent.resolve(strict=True)
    require_private_directory(control_root)
    if args.gate_fifo.parent.resolve(strict=True) != control_root:
        raise RuntimeError("launcher lease guard paths must share a private directory")
    gate_stat = args.gate_fifo.lstat()
    if not stat.S_ISFIFO(gate_stat.st_mode) or stat.S_IMODE(gate_stat.st_mode) != 0o600:
        raise RuntimeError("launcher lease guard activation gate is unsafe")
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or not Path(command[0]).is_absolute():
        raise RuntimeError("launcher lease guard command must be absolute")
    return state_root, checkout, command


def main() -> int:
    args = _parser().parse_args()
    state_root, checkout, command = _validate(args)
    _arm_controller_death(args.controller_pid)
    lease = PrimaryLauncherLease.acquire(state_root, checkout=checkout)
    try:
        args.ready_file.write_text(
            json.dumps({"lease_id": lease.lease_id, "pid": os.getpid()}),
            encoding="utf-8",
        )
        make_private_file(args.ready_file)
        with args.gate_fifo.open("r", encoding="utf-8") as gate:
            if gate.readline() != "exec\n":
                raise RuntimeError("launcher lease guard activation is invalid")
        if os.getppid() != args.controller_pid:
            raise RuntimeError("launcher lease guard controller exited")
        executable = Path(command[0]).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("launcher lease guard target is not executable")
        env = {**os.environ, **lease.prepare_handoff_environment()}
        os.execve(str(executable), command, env)
    finally:
        lease.release()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main())
