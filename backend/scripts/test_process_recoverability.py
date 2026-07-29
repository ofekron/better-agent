from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import proc_control
from proc_control import _PosixProcessControl
import run_recovery
from process_identity import process_identity_to_dict


def test_process_state_contract() -> None:
    control = _PosixProcessControl()
    expected = {
        "R": True,
        "S": True,
        "T": True,
        "U": True,
        "Z": False,
        "X": False,
    }
    for state, recoverable in expected.items():
        with (
            patch.object(control, "pid_alive", return_value=True),
            patch.object(control, "_process_state", return_value=state),
        ):
            assert control.pid_recoverable(123) is recoverable, state


def test_macos_exiting_state_is_not_recoverable() -> None:
    control = _PosixProcessControl()
    with (
        patch.object(control, "pid_alive", return_value=True),
        patch.object(control, "_process_state", return_value="UEs"),
        patch.object(proc_control.sys, "platform", "darwin"),
    ):
        assert not control.pid_recoverable(123)


def test_real_unreaped_exit_is_not_recoverable() -> None:
    if os.name == "nt":
        return
    child = os.fork()
    if child == 0:
        os._exit(0)
    try:
        os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT)
        control = _PosixProcessControl()
        assert control.pid_alive(child)
        assert not control.pid_recoverable(child)
    finally:
        os.waitpid(child, 0)


def test_recovery_requires_identity_or_cli_corroboration() -> None:
    class _Control:
        @staticmethod
        def pid_recoverable(pid: int) -> bool:
            return True

    original = run_recovery.process_control
    original_cli_identity = run_recovery._orphaned_cli_identity_live
    run_recovery.process_control = lambda: _Control()
    run_recovery._orphaned_cli_identity_live = lambda _desc, _pid: True
    try:
        assert not run_recovery._recovered_run_recoverable(
            {"pid": 123, "alive": True},
            cancelled=False,
        )
        assert run_recovery._recovered_run_recoverable(
            {
                "pid": 123,
                "cli_pid": 456,
                "alive": False,
                "orphaned_cli": True,
            },
            cancelled=False,
        )
    finally:
        run_recovery.process_control = original
        run_recovery._orphaned_cli_identity_live = original_cli_identity


def test_orphaned_cli_requires_matching_process_identity() -> None:
    identity = process_identity_to_dict(os.getpid())
    assert identity is not None
    assert not run_recovery._orphaned_cli_identity_live(
        {
            "cli_identity": identity,
            "jsonl_path": __file__,
            "jsonl_inode": os.stat(__file__).st_ino,
        },
        os.getpid() + 1,
    )


def main() -> None:
    test_process_state_contract()
    test_macos_exiting_state_is_not_recoverable()
    test_real_unreaped_exit_is_not_recoverable()
    test_recovery_requires_identity_or_cli_corroboration()
    test_orphaned_cli_requires_matching_process_identity()
    print("process recoverability tests passed")


if __name__ == "__main__":
    main()
