"""Coverage slice for process_identity.py.

PID-identity verification: a process is proven dead only when its pid no longer
exists OR its create_time changed (pid was reused by a different process).
Drives the real psutil paths against the current process and a reaped child,
plus a controlled unit-tier patch for the non-deterministic AccessDenied
fail-closed branch.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import _test_home
import psutil
import pytest

_test_home.isolate("bc-process-identity-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import process_identity  # noqa: E402
from process_identity import ProcessIdentity  # noqa: E402


def _reaped_pid() -> int:
    """Spawn a short-lived child, wait for it to exit and be reaped, return its pid.

    After reaping, psutil.Process(pid) raises NoSuchProcess (unless the OS
    reuses the pid inside the test window, which is vanishingly unlikely).
    """
    child = subprocess.Popen(["sleep", "0.15"])
    pid = child.pid
    child.wait()
    return pid


# ---------------------------------------------------------------------------
# capture_process_identity
# ---------------------------------------------------------------------------


def test_capture_rejects_non_positive_pid() -> None:
    assert process_identity.capture_process_identity(0) is None
    assert process_identity.capture_process_identity(-1) is None


def test_capture_returns_none_for_dead_pid() -> None:
    assert process_identity.capture_process_identity(_reaped_pid()) is None


def test_capture_live_process_records_create_time() -> None:
    identity = process_identity.capture_process_identity(os.getpid())
    assert identity is not None
    assert identity.pid == os.getpid()
    assert identity.create_time > 0


# ---------------------------------------------------------------------------
# process_identity_to_dict / from_dict round-trip + error branches
# ---------------------------------------------------------------------------


def test_to_dict_returns_none_when_capture_fails() -> None:
    assert process_identity.process_identity_to_dict(0) is None
    assert process_identity.process_identity_to_dict(-5) is None


def test_to_dict_roundtrips_live_process() -> None:
    as_dict = process_identity.process_identity_to_dict(os.getpid())
    assert as_dict is not None
    assert as_dict["pid"] == os.getpid()
    assert as_dict["create_time"] > 0


def test_from_dict_rejects_non_dict() -> None:
    assert process_identity.process_identity_from_dict("nope") is None
    assert process_identity.process_identity_from_dict(None) is None
    assert process_identity.process_identity_from_dict(123) is None


def test_from_dict_rejects_missing_keys() -> None:
    assert process_identity.process_identity_from_dict({"pid": 1}) is None
    assert process_identity.process_identity_from_dict({"create_time": 1.0}) is None


def test_from_dict_rejects_bad_value_types() -> None:
    assert process_identity.process_identity_from_dict({"pid": "x", "create_time": 1.0}) is None
    assert process_identity.process_identity_from_dict({"pid": 1, "create_time": "x"}) is None


def test_from_dict_rejects_non_positive_pid() -> None:
    assert process_identity.process_identity_from_dict({"pid": 0, "create_time": 1.0}) is None
    assert process_identity.process_identity_from_dict({"pid": -3, "create_time": 1.0}) is None


def test_from_dict_roundtrips_valid_payload() -> None:
    identity = process_identity.process_identity_from_dict({"pid": 7, "create_time": 12.5})
    assert identity == ProcessIdentity(pid=7, create_time=12.5)


def test_dict_roundtrip_matches_live_capture() -> None:
    captured = process_identity.capture_process_identity(os.getpid())
    assert captured is not None
    restored = process_identity.process_identity_from_dict(
        process_identity.process_identity_to_dict(os.getpid())
    )
    assert restored == captured


# ---------------------------------------------------------------------------
# process_identity_is_live
# ---------------------------------------------------------------------------


def test_is_live_true_for_live_process() -> None:
    identity = process_identity.capture_process_identity(os.getpid())
    assert identity is not None
    assert process_identity.process_identity_is_live(identity) is True


def test_is_live_false_when_create_time_changed() -> None:
    captured = process_identity.capture_process_identity(os.getpid())
    assert captured is not None
    reused = ProcessIdentity(pid=captured.pid, create_time=captured.create_time + 9999.0)
    assert process_identity.process_identity_is_live(reused) is False


# ---------------------------------------------------------------------------
# process_identity_is_proven_dead
# ---------------------------------------------------------------------------


def test_proven_dead_true_for_reaped_pid() -> None:
    identity = ProcessIdentity(pid=_reaped_pid(), create_time=1.0)
    assert process_identity.process_identity_is_proven_dead(identity) is True


def test_proven_dead_true_when_create_time_changed() -> None:
    captured = process_identity.capture_process_identity(os.getpid())
    assert captured is not None
    reused = ProcessIdentity(pid=captured.pid, create_time=captured.create_time + 9999.0)
    assert process_identity.process_identity_is_proven_dead(reused) is True


def test_proven_dead_false_for_live_same_identity() -> None:
    captured = process_identity.capture_process_identity(os.getpid())
    assert captured is not None
    assert process_identity.process_identity_is_proven_dead(captured) is False


def test_proven_dead_false_on_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the owner can't be inspected, the pid cannot be proven dead: fail closed.
    identity = process_identity.capture_process_identity(os.getpid())
    assert identity is not None

    def _raise_access_denied(_pid: int) -> "psutil.Process":
        raise psutil.AccessDenied()

    monkeypatch.setattr(process_identity.psutil, "Process", _raise_access_denied)
    assert process_identity.process_identity_is_proven_dead(identity) is False
