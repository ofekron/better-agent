"""Unit tests for requirements_search_supervisor.

Covers the deterministic decision logic of the supervised requirements-search
lane runner:
  - _failure(): per-action failure-dict construction (3 lane archetypes).
  - run_supervised_search(): argument validation + result classification.
  - _communicate_with_posix_limits(): cancel / wall / memory / rss branches.
  - _process_group_rss_kb(): ps(1) output parsing.
  - _terminate_process_tree(): posix early-return + signal catch.

The real worker subprocess (subprocess.Popen of requirements_search_worker.py)
is replaced with a FakePopen double so the supervisor's *decisions* are tested,
not the worker. Windows-only branches (_assign_windows_job, creationflags, the
nt communicate path, TerminateJobObject) are platform-dispatch exclusions and
are not exercised on posix.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Any

import pytest

import requirements_search_supervisor as sup


# ---------------------------------------------------------------- test doubles


class FakePopen:
    """Stand-in subprocess.Popen for the supervisor's process boundary.

    communicate() returns the configured stdout/stderr, optionally raising
    subprocess.TimeoutExpired on the first call to drive the memory-poll loop.
    """

    instances: list["FakePopen"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 999999  # bogus -> os.killpg raises ProcessLookupError (caught)
        self._stdout: bytes = kwargs.pop("_stdout", b"{}")
        self._stderr: bytes = kwargs.pop("_stderr", b"")
        # The supervisor reads .returncode as an attribute AFTER communicate,
        # and .poll() during the memory watch — both must agree.
        self.returncode: int | None = kwargs.pop("_returncode", 0)
        self._timeout_first: bool = kwargs.pop("_timeout_first", False)
        self.communicate_calls = 0
        self.kill_calls = 0
        type(self).instances.append(self)

    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        self.communicate_calls += 1
        if self._timeout_first and self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0.2)
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1


class FakeSubprocess:
    """Replaces the supervisor's `subprocess` module global for scoped tests."""

    PIPE = subprocess.PIPE
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    TimeoutExpired = subprocess.TimeoutExpired  # reuse the real exception class

    def __init__(self, popen_factory: Any = None, run_result: Any = None) -> None:
        self.Popen = popen_factory
        self._run_result = run_result
        self.run_calls: list[tuple[tuple, dict]] = []

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self.run_calls.append((args, kwargs))
        return self._run_result


def _install_popen(monkeypatch: pytest.MonkeyPatch, **popen_kwargs: Any) -> FakePopen:
    """Patch the supervisor's subprocess global with a FakePopen factory.

    Returns the FakePopen instance that will be constructed on the next Popen
    call so the test can assert on how the supervisor drove it.
    """
    instance = FakePopen(**popen_kwargs)

    def _factory(*args: Any, **kwargs: Any) -> FakePopen:
        instance.args = args
        instance.kwargs = kwargs
        return instance

    fake = FakeSubprocess(popen_factory=_factory)
    monkeypatch.setattr(sup, "subprocess", fake)
    return instance


# ---------------------------------------------------------------- _failure


def test_failure_normal_lane_has_retry_hint_and_narrow_query() -> None:
    result = sup._failure("unit_rg", "CPU", "20s")
    assert result["success"] is False
    assert result["error_code"] == "requirements_search_resource_limit"
    assert result["lane"] == "unit-rg"
    assert result["resource"] == "CPU"
    assert "exceeded its CPU limit (20s)" in result["error"]
    assert "Retry with a finer search" in result["error"]
    assert result["retryable"] is True
    assert result["retry_strategy"] == "narrow_query"


def test_failure_thread_vector_search_is_not_retryable_and_no_strategy() -> None:
    result = sup._failure("thread_vector_search", "memory", "1024MB")
    assert result["lane"] == "thread-vector-search"
    assert result["retryable"] is False
    assert "retry_strategy" not in result
    assert "Retry with a finer search" not in result["error"]


def test_failure_projection_build_uses_resume_strategy() -> None:
    result = sup._failure("thread_vector_build", "wall-time", "240s")
    assert result["lane"] == "thread-vector-build"
    assert result["retryable"] is True
    assert result["retry_strategy"] == "resume_projection_build"
    assert "Retry with a finer search" not in result["error"]


# ---------------------------------------------------------------- validation


def test_run_supervised_search_rejects_unknown_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Popen must never be reached for an unknown action.
    _install_popen(monkeypatch)
    with pytest.raises(ValueError, match="unsupported requirements search action"):
        sup.run_supervised_search("nope_such_lane")


def test_run_supervised_search_rejects_non_event_cancel_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch)
    with pytest.raises(TypeError, match="_cancel_event must be a threading.Event"):
        sup.run_supervised_search("unit_rg", _cancel_event="not-an-event")


def test_run_supervised_search_accepts_real_event_then_validates_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch)
    with pytest.raises(ValueError, match="unsupported requirements search action"):
        sup.run_supervised_search(
            "nope_such_lane", _cancel_event=threading.Event()
        )


# ---------------------------------------------------------------- result classification


def test_run_supervised_search_injects_cpu_limit_env_and_start_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _install_popen(monkeypatch, _stdout=b'{"ok": true}', _returncode=0)
    result = sup.run_supervised_search("unit_rg", query="auth")
    assert result == {"ok": True}
    launch = proc.kwargs
    assert launch["env"]["BETTER_AGENT_SEARCH_CPU_SECONDS"] == "20"
    assert launch["start_new_session"] is True  # posix launch flag


def test_run_supervised_search_returns_worker_result_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch, _stdout=b'{"hits": [1, 2]}', _returncode=0)
    result = sup.run_supervised_search("unit_fts", q="x")
    assert result == {"hits": [1, 2]}


def test_run_supervised_search_classifies_exceeded_from_communicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch)
    exceeded = sup.SupervisedOutput(b"", b"", "memory", "512MB")
    monkeypatch.setattr(
        sup, "_communicate_with_limits", lambda *a, **kw: exceeded
    )
    result = sup.run_supervised_search("unit_rg")
    assert result["resource"] == "memory"
    assert result["retryable"] is True
    assert result["retry_strategy"] == "narrow_query"


def test_run_supervised_search_rejects_oversize_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(
        monkeypatch, _stdout=b"x" * (sup._MAX_RESULT_BYTES + 1), _returncode=0
    )
    result = sup.run_supervised_search("unit_rg")
    assert result["resource"] == "output"
    assert f"{sup._MAX_RESULT_BYTES // (1024 * 1024)}MB)" in result["error"]


def test_run_supervised_search_classifies_sigxcpu_as_cpu_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch, _stdout=b"", _stderr=b"", _returncode=-signal.SIGXCPU)
    result = sup.run_supervised_search("unit_rg")
    assert result["resource"] == "CPU"
    assert "(20s)" in result["error"]


def test_run_supervised_search_classifies_sigkill_as_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch, _stdout=b"", _stderr=b"killed", _returncode=-signal.SIGKILL)
    result = sup.run_supervised_search("unit_rg")
    assert result["resource"] == "memory"
    assert "(512MB)" in result["error"]


def test_run_supervised_search_classifies_memory_error_stderr_as_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(
        monkeypatch, _stdout=b"", _stderr=b"Traceback: MemoryError", _returncode=1
    )
    result = sup.run_supervised_search("unit_rg")
    assert result["resource"] == "memory"


def test_run_supervised_search_generic_failure_includes_stderr_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(
        monkeypatch, _stdout=b"", _stderr=b"  boom: it broke  ", _returncode=1
    )
    result = sup.run_supervised_search("unit_rg")
    assert result["error_code"] == "requirements_search_worker_failed"
    assert result["lane"] == "unit-rg"
    assert result["retryable"] is False
    assert "boom: it broke" in result["error"]


def test_run_supervised_search_generic_failure_with_empty_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch, _stdout=b"", _stderr=b"", _returncode=1)
    result = sup.run_supervised_search("unit_rg")
    assert "worker exited unexpectedly" in result["error"]


def test_run_supervised_search_invalid_json_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch, _stdout=b"not-json{", _returncode=0)
    result = sup.run_supervised_search("unit_rg")
    assert result["error_code"] == "requirements_search_invalid_result"
    assert result["retryable"] is False


def test_run_supervised_search_non_object_result_raises_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_popen(monkeypatch, _stdout=b"[1, 2, 3]", _returncode=0)
    with pytest.raises(TypeError, match="must be an object"):
        sup.run_supervised_search("unit_rg")


# ---------------------------------------------------------------- _communicate_with_posix_limits


def _fake_alive_process(**kw: Any) -> FakePopen:
    return FakePopen(_returncode=None, **kw)


def test_communicate_posix_happy_path_returns_output() -> None:
    proc = FakePopen(_stdout=b"out", _stderr=b"err", _returncode=0)
    out = sup._communicate_with_posix_limits(proc, b"payload", sup.SEARCH_LIMITS["unit_rg"], None)
    assert out == sup.SupervisedOutput(b"out", b"err")


def test_communicate_posix_cancel_event_terminates_and_flags_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_alive_process(_stdout=b"o", _stderr=b"e")
    terminated: list = []
    monkeypatch.setattr(sup, "_terminate_process_tree", lambda p, job: terminated.append(p))
    cancel = threading.Event()
    cancel.set()
    out = sup._communicate_with_posix_limits(
        proc, b"payload", sup.SEARCH_LIMITS["unit_rg"], None, cancel
    )
    assert out.exceeded_resource == "cancelled"
    assert out.exceeded_limit == "shutdown"
    assert terminated == [proc]


def test_communicate_posix_wall_deadline_flags_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_alive_process(_stdout=b"o", _stderr=b"e")
    terminated: list = []
    monkeypatch.setattr(sup, "_terminate_process_tree", lambda p, job: terminated.append(p))
    # Advancing clock: deadline is set from the first reading; the second
    # reading jumps past it so `remaining = deadline - now` <= 0 on the first
    # loop check (before communicate is reached).
    ticks = {"n": 0}

    def _advancing() -> float:
        ticks["n"] += 1
        return 1000.0 + ticks["n"] * 1000.0

    monkeypatch.setattr(sup.time, "monotonic", _advancing)
    out = sup._communicate_with_posix_limits(
        proc, b"payload", sup.SEARCH_LIMITS["unit_rg"], None
    )
    assert out.exceeded_resource == "wall-time"
    assert out.exceeded_limit == "30s"
    assert terminated == [proc]


def test_communicate_posix_memory_exceeded_via_rss_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_alive_process(_stdout=b"o", _stderr=b"e", _timeout_first=True)
    terminated: list = []
    monkeypatch.setattr(sup, "_terminate_process_tree", lambda p, job: terminated.append(p))
    # unit_rg memory limit is 512MB == 524288 kB; report above it.
    monkeypatch.setattr(sup, "_process_group_rss_kb", lambda pid: 600000)
    out = sup._communicate_with_posix_limits(
        proc, b"payload", sup.SEARCH_LIMITS["unit_rg"], None
    )
    assert out.exceeded_resource == "memory"
    assert out.exceeded_limit == "512MB"
    assert terminated == [proc]


def test_communicate_posix_memory_exceeded_via_unknown_rss_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_alive_process(_stdout=b"o", _stderr=b"e", _timeout_first=True)
    terminated: list = []
    monkeypatch.setattr(sup, "_terminate_process_tree", lambda p, job: terminated.append(p))
    monkeypatch.setattr(sup, "_process_group_rss_kb", lambda pid: None)
    out = sup._communicate_with_posix_limits(
        proc, b"payload", sup.SEARCH_LIMITS["unit_rg"], None
    )
    assert out.exceeded_resource == "memory"


def test_communicate_posix_rss_within_limit_keeps_running_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_alive_process(_stdout=b"done", _stderr=b"", _timeout_first=True)
    monkeypatch.setattr(sup, "_process_group_rss_kb", lambda pid: 100000)  # < 512MB
    monkeypatch.setattr(sup, "_terminate_process_tree", lambda p, job: None)
    out = sup._communicate_with_posix_limits(
        proc, b"payload", sup.SEARCH_LIMITS["unit_rg"], None
    )
    assert out == sup.SupervisedOutput(b"done", b"")
    assert proc.communicate_calls == 2  # first timed out, second returned


# ---------------------------------------------------------------- _process_group_rss_kb


def _completed(stdout: str, returncode: int = 0) -> Any:
    obj = type("Result", (), {})()
    obj.stdout = stdout
    obj.stderr = ""
    obj.returncode = returncode
    return obj


def test_process_group_rss_kb_sums_matching_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    pgid = os.getpgid(os.getpid())
    fake = FakeSubprocess(
        run_result=_completed(f"  {pgid} 12345\n   7 999\n  {pgid} 5\n")
    )
    monkeypatch.setattr(sup, "subprocess", fake)
    assert sup._process_group_rss_kb(os.getpid()) == 12350
    assert len(fake.run_calls) == 1


def test_process_group_rss_kb_skips_malformed_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgid = os.getpgid(os.getpid())
    fake = FakeSubprocess(
        run_result=_completed(
            f"toomany fields here\n  bad rss\n  {pgid} 7\n  {pgid} notanumber\n"
        )
    )
    monkeypatch.setattr(sup, "subprocess", fake)
    assert sup._process_group_rss_kb(os.getpid()) == 7


def test_process_group_rss_kb_none_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSubprocess(run_result=_completed("  1234 999\n  5678 8"))
    monkeypatch.setattr(sup, "subprocess", fake)
    assert sup._process_group_rss_kb(os.getpid()) is None


def test_process_group_rss_kb_none_when_ps_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSubprocess(run_result=_completed("", returncode=1))
    monkeypatch.setattr(sup, "subprocess", fake)
    assert sup._process_group_rss_kb(os.getpid()) is None


def test_process_group_rss_kb_none_when_pid_gone() -> None:
    # A nonexistent pid makes os.getpgid raise ProcessLookupError naturally.
    assert sup._process_group_rss_kb(99999999) is None


# ---------------------------------------------------------------- _terminate_process_tree


def test_terminate_process_tree_noop_when_process_already_exited() -> None:
    proc = FakePopen(_returncode=0)
    # Must not raise and must not attempt any signal.
    sup._terminate_process_tree(proc, None)
    assert proc.kill_calls == 0


def test_terminate_process_tree_swallows_processlookuperror_for_bogus_pg() -> None:
    proc = _fake_alive_process()  # pid 999999 -> killpg raises ProcessLookupError
    # Real os.killpg on a nonexistent process group raises ProcessLookupError,
    # which the posix branch must swallow. No real signal is delivered.
    sup._terminate_process_tree(proc, None)
    assert proc.kill_calls == 0  # posix branch uses killpg, not .kill()
