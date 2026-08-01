"""Full unit coverage for runner_exit.py.

``hard_exit`` tears down a runner process and exits immediately via
``os._exit`` so that blocked runtime-operation threads cannot wedge the
native-session wind-down gate. It is defence-in-depth for process liveness.

The existing ``test_runner_hard_exit.py`` / ``test_runner_alive_outlives_complete.py``
prove the behaviour by spawning real subprocesses that call ``os._exit``; the
parent pytest process therefore never imports ``runner_exit`` and captures no
coverage of it (same shape as prompt_templates in run #75). These tests drive
every teardown branch in-process by replacing ``os._exit`` and the operation
host / stream boundaries, so the branches are measured deterministically
without killing the test interpreter.

The flush loop iterates ``for stream in (sys.stdout, sys.stderr)`` reading the
``sys`` attributes at call time, so we swap ``sys.stdout``/``sys.stderr`` for
fake stream *objects*. That isolates the explicit loop from ``logging.shutdown``
(which flushes handlers that reference the original stream objects captured at
handler creation).
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runner_exit  # noqa: E402
import runner_operation_host  # noqa: E402


class _Exited(Exception):
    """Stands in for os._exit so the call is observable without dying."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _FakeStream:
    def __init__(self, *, on_flush=None):
        self.flush_calls = 0
        self._on_flush = on_flush

    def flush(self) -> None:
        self.flush_calls += 1
        if self._on_flush is not None:
            self._on_flush()


def _install_exit_sentinel(monkeypatch) -> list[int]:
    codes: list[int] = []

    def _exit(code: int) -> None:  # os._exit is NoReturn in spirit
        codes.append(code)
        raise _Exited(code)

    monkeypatch.setattr(os, "_exit", _exit)
    return codes


def _stub_logging_shutdown(monkeypatch) -> list[bool]:
    calls: list[bool] = []
    # logging.shutdown() also flushes handlers that alias sys.stderr
    # dynamically (_StderrHandler/lastResort); stubbing it isolates the
    # explicit flush loop, which is the branch under test here. L40 still
    # executes (the call statement runs).
    monkeypatch.setattr(logging, "shutdown", lambda: calls.append(True))
    return calls


def test_hard_exit_runs_full_teardown_then_exits(monkeypatch):
    host_calls: list[bool] = []
    monkeypatch.setattr(
        runner_operation_host, "stop_active_host", lambda: host_calls.append(True)
    )
    fake_out, fake_err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    shutdown_calls = _stub_logging_shutdown(monkeypatch)
    codes = _install_exit_sentinel(monkeypatch)

    with pytest.raises(_Exited) as exc:
        runner_exit.hard_exit(3)
    assert exc.value.code == 3
    assert codes == [3]
    assert host_calls == [True]
    assert shutdown_calls == [True]
    assert fake_out.flush_calls == 1
    assert fake_err.flush_calls == 1


def test_hard_exit_survives_operation_host_teardown_failure(monkeypatch, caplog):
    def _boom() -> None:
        raise RuntimeError("host down")

    monkeypatch.setattr(runner_operation_host, "stop_active_host", _boom)
    monkeypatch.setattr(sys, "stdout", _FakeStream())
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    _stub_logging_shutdown(monkeypatch)
    _install_exit_sentinel(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="runner_exit"):
        with pytest.raises(_Exited):
            runner_exit.hard_exit(0)
    assert any(
        "operation host teardown failed" in record.message
        for record in caplog.records
    )


def test_hard_exit_swallows_stream_flush_failure(monkeypatch):
    monkeypatch.setattr(runner_operation_host, "stop_active_host", lambda: None)

    def _broken_flush() -> None:
        raise OSError("broken pipe")

    fake_out = _FakeStream(on_flush=_broken_flush)
    fake_err = _FakeStream()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    _stub_logging_shutdown(monkeypatch)
    codes = _install_exit_sentinel(monkeypatch)

    # stdout flush failure is swallowed; hard_exit still flushes stderr and
    # reaches os._exit(1).
    with pytest.raises(_Exited):
        runner_exit.hard_exit(1)
    assert fake_err.flush_calls == 1
    assert codes == [1]


if __name__ == "__main__":
    pytest.main([__file__, "-q", "-p", "no:cacheprovider"])
