"""Unit tests for requirements_search_worker.

Covers the pure POSIX-rlimit helper layer and the main() dispatch wiring.
The real requirement_context search backends are stubbed via sys.modules so
main()'s registry-consistency, validation, and dispatch paths are exercised
without spending any real search work; those backends belong to the
full-stack tier.
"""

from __future__ import annotations

import io
import json
import resource
import sys
import types
from typing import Any

import pytest

import requirements_search_worker as worker


# ---------------------------------------------------------------- rlimit stub


class FakeResource:
    """Stand-in for the stdlib `resource` module.

    Passed explicitly into helpers that take a resource_module parameter, and
    injected into sys.modules for the _apply_posix_limits posix path so the
    real process rlimits are never mutated by the test suite.
    """

    RLIM_INFINITY = resource.RLIM_INFINITY
    RLIMIT_CPU = resource.RLIMIT_CPU
    RLIMIT_NOFILE = 7

    def __init__(
        self,
        getrlimit_returns: tuple[int, int] = (100, 200),
        setrlimit_raises: BaseException | None = None,
    ) -> None:
        # Constants are set as INSTANCE attributes because _resource_limit_name
        # iterates vars(resource_module) (instance __dict__), not dir().
        self.RLIM_INFINITY = resource.RLIM_INFINITY
        self.RLIMIT_CPU = resource.RLIMIT_CPU
        self.RLIMIT_NOFILE = 7
        self._get = getrlimit_returns
        self._raises = setrlimit_raises
        self.setrlimit_calls: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        self.getrlimit_calls = getattr(self, "getrlimit_calls", [])
        return self._get

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        self.setrlimit_calls.append((which, limits))
        if self._raises is not None:
            raise self._raises


# ---------------------------------------------------------------- _bounded_limit


def test_bounded_limit_returns_configured_when_hard_is_infinity() -> None:
    assert worker._bounded_limit(30, resource.RLIM_INFINITY) == 30


def test_bounded_limit_caps_at_finite_hard() -> None:
    assert worker._bounded_limit(30, 200) == 30
    assert worker._bounded_limit(300, 200) == 200


# ---------------------------------------------------------------- _resource_limit_name


def test_resource_limit_name_resolves_known_constant() -> None:
    fr = FakeResource()
    assert worker._resource_limit_name(fr, FakeResource.RLIMIT_CPU) == "RLIMIT_CPU"
    assert worker._resource_limit_name(fr, FakeResource.RLIMIT_NOFILE) == "RLIMIT_NOFILE"


def test_resource_limit_name_fallback_for_unknown_constant() -> None:
    fr = FakeResource()
    assert worker._resource_limit_name(fr, 999) == "resource limit 999"


# ---------------------------------------------------------------- _format_limit_value / _format_limit_pair


def test_format_limit_value_humanizes_each_case() -> None:
    fr = FakeResource()
    assert worker._format_limit_value(fr, fr.RLIM_INFINITY) == "unlimited"
    assert worker._format_limit_value(fr, -5) == "unlimited"
    assert worker._format_limit_value(fr, 30) == "30s"


def test_format_limit_pair_composes_soft_and_hard() -> None:
    fr = FakeResource()
    pair = worker._format_limit_pair(fr, 30, fr.RLIM_INFINITY)
    assert pair == "soft=30s hard=unlimited"


# ---------------------------------------------------------------- _set_posix_limit


def test_set_posix_limit_succeeds_silently_and_records_call() -> None:
    fr = FakeResource(getrlimit_returns=(10, 20))
    worker._set_posix_limit(fr, fr.RLIMIT_CPU, "CPU", 30, 31, 10, 20)
    assert fr.setrlimit_calls == [(fr.RLIMIT_CPU, (30, 31))]


@pytest.mark.parametrize("exc", [OSError("boom"), ValueError("nope")])
def test_set_posix_limit_wraps_oserror_valueerror_as_runtimeerror(
    exc: BaseException,
) -> None:
    fr = FakeResource(setrlimit_raises=exc)
    with pytest.raises(RuntimeError) as info:
        worker._set_posix_limit(fr, fr.RLIMIT_CPU, "CPU", 30, 31, 10, 20)
    msg = str(info.value)
    # The formatted diagnostic names the resource, the limit constant, the
    # requested + inherited pairs, and chains the original system error.
    assert "POSIX CPU limit" in msg
    assert "RLIMIT_CPU" in msg
    assert "requested soft=30s" in msg
    assert "inherited soft=10s" in msg
    assert str(exc) in msg


def test_set_posix_limit_error_uses_fallback_name_for_unknown_constant() -> None:
    fr = FakeResource(setrlimit_raises=OSError("x"))
    with pytest.raises(RuntimeError) as info:
        worker._set_posix_limit(fr, 999, "FROB", 1, 2, 3, 4)
    assert "resource limit 999" in str(info.value)


# ---------------------------------------------------------------- _apply_posix_limits


def test_apply_posix_limits_noop_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    setrlimit_calls: list[Any] = []

    fake = FakeResource()
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setenv("BETTER_AGENT_SEARCH_CPU_SECONDS", "30")

    worker._apply_posix_limits()  # must not touch rlimits on win32
    assert fake.setrlimit_calls == []
    assert setrlimit_calls == []


def test_apply_posix_limits_bounded_to_inherited_hard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # configured CPU seconds exceed the inherited hard limit -> soft/hard cap
    # at the inherited hard limit, never raising it.
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeResource(getrlimit_returns=(5, 20))
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setenv("BETTER_AGENT_SEARCH_CPU_SECONDS", "300")

    worker._apply_posix_limits()

    assert fake.setrlimit_calls == [
        (fake.RLIMIT_CPU, (min(300, 20), min(301, 20))),
    ]


def test_apply_posix_limits_keeps_configured_when_hard_infinite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeResource(getrlimit_returns=(5, resource.RLIM_INFINITY))
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setenv("BETTER_AGENT_SEARCH_CPU_SECONDS", "42")

    worker._apply_posix_limits()

    assert fake.setrlimit_calls == [(fake.RLIMIT_CPU, (42, 43))]


def test_apply_posix_limits_propagates_setrlimit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeResource(
        getrlimit_returns=(5, 20), setrlimit_raises=OSError("denied")
    )
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setenv("BETTER_AGENT_SEARCH_CPU_SECONDS", "30")

    with pytest.raises(RuntimeError) as info:
        worker._apply_posix_limits()
    assert "POSIX CPU limit" in str(info.value)


# ---------------------------------------------------------------- main()


def _make_fake_requirement_context(captured: dict[str, Any]) -> types.ModuleType:
    """Build a fake requirement_context exposing all 7 worker action callables.

    Each callable records the kwargs it was called with under its action name
    and returns a distinguishable result dict.
    """
    mod = types.ModuleType("requirement_context")

    def _make(action: str):
        def _fn(**kwargs: Any) -> dict[str, Any]:
            captured["calls"].append((action, kwargs))
            return {"action": action, "echo": kwargs}

        return _fn

    mod.search_requirements = _make("unit_rg")
    mod.search_requirement_units_fts = _make("unit_fts")
    mod.search_requirement_threads_fts = _make("thread_fts")
    mod.search_requirement_units_vector = _make("unit_vector")
    mod.search_requirement_threads_vector = _make("thread_vector_search")
    mod.build_requirement_threads_vector_projection = _make("thread_vector_build")
    mod.run_native_index_sql = _make("index_sql")
    return mod


def test_main_returns_two_when_posix_limits_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise() -> None:
        raise RuntimeError("limit boom")

    monkeypatch.setattr(worker, "_apply_posix_limits", _raise)
    # The real requirement_context backends must not be needed on the early
    # exit path; ensure a stale module from another test can't satisfy the
    # later import so we prove main() never reaches it.
    monkeypatch.setitem(sys.modules, "requirement_context", None)

    assert worker.main() == 2
    assert "limit boom" in capsys.readouterr().err


def test_main_dispatches_valid_request_and_dumps_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker, "_apply_posix_limits", lambda: None)
    captured: dict[str, Any] = {"calls": []}
    monkeypatch.setitem(
        sys.modules, "requirement_context", _make_fake_requirement_context(captured)
    )
    request = {"action": "unit_rg", "kwargs": {"query": "auth", "limit": 5}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))

    rc = worker.main()

    assert rc == 0
    assert captured["calls"] == [("unit_rg", {"query": "auth", "limit": 5})]
    out = capsys.readouterr().out
    # compact separators, unicode-preserved, exact echo of the returned dict.
    assert json.loads(out) == {"action": "unit_rg", "echo": {"query": "auth", "limit": 5}}
    assert ", " not in out  # separators=(",", ":")


@pytest.mark.parametrize(
    "request_payload",
    [
        {"action": "unit_rg"},                       # missing kwargs
        {"action": "nope", "kwargs": {}},            # unknown action
        {"action": "unit_rg", "kwargs": "notdict"},  # kwargs not a dict
    ],
)
def test_main_rejects_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
    request_payload: dict[str, Any],
) -> None:
    monkeypatch.setattr(worker, "_apply_posix_limits", lambda: None)
    captured: dict[str, Any] = {"calls": []}
    monkeypatch.setitem(
        sys.modules, "requirement_context", _make_fake_requirement_context(captured)
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request_payload)))

    with pytest.raises(ValueError, match="invalid requirements search worker request"):
        worker.main()
    assert captured["calls"] == []  # nothing dispatched on rejection


def test_main_rejects_inconsistent_action_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "_apply_posix_limits", lambda: None)
    captured: dict[str, Any] = {"calls": []}
    fake = _make_fake_requirement_context(captured)
    monkeypatch.setitem(sys.modules, "requirement_context", fake)
    # The actions dict is built from literal keys that always match the
    # contract, so the only way the set-inequality guard can fire is a drift
    # between WORKER_ACTION_NAMES and those literal keys. Patch the contract
    # to a reduced set to prove the guard raises.
    monkeypatch.setattr(worker, "WORKER_ACTION_NAMES", ("unit_rg",))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"action": "unit_rg", "kwargs": {}}))
    )

    with pytest.raises(RuntimeError, match="action registry is inconsistent"):
        worker.main()
    assert captured["calls"] == []  # guard fires before any dispatch
