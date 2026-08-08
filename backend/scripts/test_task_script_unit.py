"""Unit owner driving task_script.py to 100% coverage.

task_script is the single canonical runner for task-related subprocess scripts:
it always runs an argv list (never a shell string) and inherits only a curated
env, so a user-authored script cannot inject commands or exfiltrate backend
secrets. These tests lock every branch — input guards, success, output cap,
timeout, OS/value-error encoding, and the run_scripts multi-script loop.

Reachable paths use real subprocesses (the test interpreter, via sys.executable).
The bytes-stdout and ValueError defensive branches cannot be produced by
subprocess.run from validated argv (text=True always yields str; a checked
list command never raises ValueError), so they are exercised by injecting the
exact exception shape — locking the contract that such inputs are encoded, never
crash the caller.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import task_script  # noqa: E402


def _py(args: list[str]) -> list[str]:
    """An argv list running the test interpreter with -c (hermetic, no PATH)."""
    return [sys.executable, "-c", *args]


# --- run_script input guards -------------------------------------------------

def test_run_script_none_returns_none():
    assert task_script.run_script(None) is None


def test_run_script_empty_dict_returns_none():
    assert task_script.run_script({}) is None


def test_run_script_non_dict_returns_none():
    # truthy but not a dict — the isinstance guard must reject before .get crashes
    assert task_script.run_script("not-a-dict") is None


def test_run_script_command_not_a_list_returns_none():
    assert task_script.run_script({"command": "echo hi"}) is None


def test_run_script_empty_command_returns_none():
    assert task_script.run_script({"command": []}) is None


# --- run_script success + output cap ---------------------------------------

def test_run_script_success_returns_result():
    res = task_script.run_script({"command": _py(["print('hi')"])})
    assert res.ok
    assert res.exit_code == 0
    assert res.timed_out is False
    assert "hi" in res.stdout


def test_run_script_truncates_output_at_max():
    # unbounded script output must be capped, not passed through
    res = task_script.run_script(
        {"command": _py([f"import sys; sys.stdout.write('x' * {task_script._MAX_OUTPUT + 50})"])}
    )
    assert len(res.stdout) == task_script._MAX_OUTPUT
    assert len(res.stderr) == 0


# --- run_script timeout -----------------------------------------------------

def test_run_script_timeout_real_subprocess():
    # a real child exceeding the timeout is killed and encoded as timed_out
    res = task_script.run_script({"command": _py(["import time; time.sleep(30)"])}, timeout=1)
    assert res.timed_out is True
    assert res.exit_code == 124
    assert res.ok is False


def test_run_script_timeout_non_str_outputs_coerce_to_empty(monkeypatch):
    # subprocess.run(text=True) always yields str; the defensive non-str
    # (bytes) branches must coerce to "" and fall back to "timed out" on empty.
    err = subprocess.TimeoutExpired(cmd=["x"], timeout=1)
    err.stdout = b"bytes-out"
    err.stderr = b"bytes-err"

    def _raise(*_a, **_k):
        raise err

    monkeypatch.setattr(task_script.subprocess, "run", _raise)
    res = task_script.run_script({"command": ["x"]})
    assert res.timed_out is True
    assert res.exit_code == 124
    assert res.stdout == ""          # bytes coerced to ""
    assert res.stderr == "timed out"  # empty-after-coerce → fallback


# --- run_script OS / value errors ------------------------------------------

def test_run_script_missing_binary_returns_126():
    res = task_script.run_script({"command": ["/nonexistent/binary_xyz_123"]})
    assert res.exit_code == 126
    assert res.ok is False
    assert res.timed_out is False
    assert res.stdout == ""
    assert res.stderr  # carries the OSError message


def test_run_script_value_error_returns_126(monkeypatch):
    # ValueError cannot arise from validated argv via real subprocess; lock that
    # it is caught and encoded rather than crashing the caller.
    def _raise(*_a, **_k):
        raise ValueError("bad value")

    monkeypatch.setattr(task_script.subprocess, "run", _raise)
    res = task_script.run_script({"command": ["x"]})
    assert res.exit_code == 126
    assert res.ok is False
    assert "bad value" in res.stderr


# --- run_scripts loop -------------------------------------------------------

def test_run_scripts_empty_is_ok():
    assert task_script.run_scripts([]) == (True, "")


def test_run_scripts_skips_none_results():
    # {} → run_script returns None → skipped, nothing combined
    assert task_script.run_scripts([{}]) == (True, "")


def test_run_scripts_appends_stdout_and_continues():
    # first script emits no stdout (append-guard False), second emits stdout
    # (append-guard True); both ok → joined result
    ok, combined = task_script.run_scripts([
        {"command": _py(["pass"])},        # no stdout, exit 0
        {"command": _py(["print('done')"])},  # stdout, exit 0
    ])
    assert ok is True
    assert "done" in combined


def test_run_scripts_stops_on_first_failure():
    ok, combined = task_script.run_scripts([
        {"command": _py(["print('before')"])},     # ok, stdout appended
        {"command": _py(["raise SystemExit(7)"])},  # fails → stop
        {"command": _py(["print('after')"])},       # never runs
    ])
    assert ok is False
    assert "before" in combined
    assert "after" not in combined
