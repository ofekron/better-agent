from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

import _test_home

_test_home.isolate("bc-test-model-catalog-engine-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import model_catalog_refresh_engine as mce  # noqa: E402
from codex_model_discovery import DiscoveryResult, SourceInspection  # noqa: E402
from model_catalog_refresh_engine import CatalogRefreshEngine  # noqa: E402
from model_catalog_refresh_state import CatalogProjection, changed_fact  # noqa: E402

# Backend async tests run under pytest-anyio (the anyio plugin provides the marker).
pytestmark = pytest.mark.anyio


@pytest.fixture
def eng() -> CatalogRefreshEngine:
    return CatalogRefreshEngine(
        wake=MagicMock(),
        fact_sink=None,
        max_age_seconds=100.0,
        retry_base_seconds=10.0,
        retry_max_seconds=300.0,
        max_concurrency=4,
    )


def _freeze_time(monkeypatch, now: float) -> None:
    # Replace only this module's `time` reference so time.time() is deterministic
    # without disturbing the global time module used by pytest/coverage.
    monkeypatch.setattr(mce, "time", types.SimpleNamespace(time=lambda: now))


# --- catalog_providers: static pure filter ---

def test_catalog_providers_rejects_non_list_state() -> None:
    assert CatalogRefreshEngine.catalog_providers({}) == []
    assert CatalogRefreshEngine.catalog_providers({"providers": None}) == []
    assert CatalogRefreshEngine.catalog_providers({"providers": "nope"}) == []


def test_catalog_providers_keeps_only_unsuspended_codex() -> None:
    state = {
        "providers": [
            {"id": "a", "kind": "codex", "generation": "g1"},
            {"id": "b", "kind": "claude", "generation": "g2"},  # wrong kind
            {"id": "c", "kind": "codex", "suspended": True, "generation": "g3"},  # suspended
            {"id": "d", "kind": "codex", "suspended": False, "generation": "g4"},
            "not-a-dict",  # non-dict item
        ]
    }
    result = CatalogRefreshEngine.catalog_providers(state)
    assert [p["id"] for p in result] == ["a", "d"]


def test_catalog_providers_returns_copies_not_aliases() -> None:
    original = {"id": "a", "kind": "codex", "generation": "g1", "extra": 1}
    [got] = CatalogRefreshEngine.catalog_providers({"providers": [original]})
    got["id"] = "mutated"
    assert original["id"] == "a"  # source dict untouched


# --- next_timeout ---

def test_next_timeout_none_when_nothing_scheduled(eng: CatalogRefreshEngine) -> None:
    assert eng.next_timeout() is None


def test_next_timeout_clamps_negative_to_zero(eng, monkeypatch) -> None:
    eng._next_due = {"a": 100.0}
    _freeze_time(monkeypatch, 200.0)
    assert eng.next_timeout() == 0.0


def test_next_timeout_returns_min_remaining(eng, monkeypatch) -> None:
    eng._next_due = {"a": 150.0, "b": 300.0}
    _freeze_time(monkeypatch, 100.0)
    assert eng.next_timeout() == 50.0


# --- _schedule_retry: exponential backoff, clamping, wake ---

def test_schedule_retry_first_attempt_uses_base_delay(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 1000.0)
    eng._schedule_retry("p1")  # 10 * 2^0 = 10
    assert eng._failures["p1"] == 1
    assert eng._next_due["p1"] == 1010.0
    eng._wake.assert_called_once()


def test_schedule_retry_doubles_each_attempt(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    eng._schedule_retry("p1")  # 10
    eng._schedule_retry("p1")  # 20
    eng._schedule_retry("p1")  # 40
    assert eng._failures["p1"] == 3
    assert eng._next_due["p1"] == 40.0


def test_schedule_retry_clamps_to_max(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    eng._failures["p1"] = 100  # 2^min(99,16) vastly exceeds retry_max
    eng._schedule_retry("p1")
    assert eng._next_due["p1"] == eng._retry_max_seconds


# --- _is_live ---

def test_is_live_generation_match(eng: CatalogRefreshEngine) -> None:
    eng._live_generations = {"p1": "g1"}
    assert eng._is_live({"id": "p1", "generation": "g1"}) is True
    assert eng._is_live({"id": "p1", "generation": "g2"}) is False  # stale generation
    assert eng._is_live({"id": "p9", "generation": "g1"}) is False  # unknown provider


# --- _projection: builder over snapshot/authority presence ---

def test_projection_without_snapshot_is_empty(eng: CatalogRefreshEngine) -> None:
    eng._snapshots = {}
    p = eng._projection(
        {"id": "p1", "generation": "g1"}, status="pending", reason="catalog_pending"
    )
    assert (p.provider_id, p.provider_generation) == ("p1", "g1")
    assert p.models == ()
    assert p.models_current is False
    assert p.retired == ()
    assert p.last_refreshed_at == 0.0
    assert p.authority_fingerprint == ""


def test_projection_models_current_requires_snapshot(eng: CatalogRefreshEngine) -> None:
    eng._snapshots = {}
    p = eng._projection(
        {"id": "p1", "generation": "g1"}, status="current", reason="", models_current=True
    )
    assert p.models_current is False  # requested True but no snapshot -> False


def test_projection_with_snapshot_and_authority(eng: CatalogRefreshEngine) -> None:
    snap = types.SimpleNamespace(
        models=("m1", "m2"), retired=("r1",), last_refreshed_at=42.0
    )
    eng._snapshots = {"p1": snap}
    auth = types.SimpleNamespace(fingerprint="fp9")
    p = eng._projection(
        {"id": "p1", "generation": "g1"},
        status="current",
        reason="",
        authority=auth,
        models_current=True,
    )
    assert p.models == ("m1", "m2")
    assert p.models_current is True
    assert p.retired == ("r1",)
    assert p.last_refreshed_at == 42.0
    assert p.authority_fingerprint == "fp9"


# --- accessors ---

def test_authorities_returns_an_independent_copy(eng: CatalogRefreshEngine) -> None:
    eng._authorities = {"p1": "auth-a"}
    got = eng.authorities
    got["p2"] = "x"
    assert eng._authorities == {"p1": "auth-a"}


def test_snapshot_returns_none_when_absent(eng: CatalogRefreshEngine) -> None:
    assert eng.snapshot("p1") is None


def test_snapshot_returns_stored_projection(eng: CatalogRefreshEngine) -> None:
    marker = object()
    eng._projections = {"p1": marker}
    assert eng.snapshot("p1") is marker


def test_bind_wake_rebinds_callback(eng: CatalogRefreshEngine) -> None:
    calls: list[str] = []
    eng.bind_wake(lambda: calls.append("new"))
    eng._wake()
    assert calls == ["new"]


# --- async helpers: _emit, _project, failure publishers (no real I/O) ---

def _projection(
    provider_id: str = "p1",
    generation: str = "g1",
    status: str = "pending",
    reason: str = "",
) -> CatalogProjection:
    return CatalogProjection(
        provider_id=provider_id,
        provider_generation=generation,
        status=status,
        models=(),
        models_current=False,
        retired=(),
        last_refreshed_at=0.0,
        reason=reason,
        authority_fingerprint="",
    )


def _live(eng: CatalogRefreshEngine, provider_id: str = "p1", generation: str = "g1") -> None:
    eng._live_generations = {provider_id: generation}


async def test_emit_noop_when_no_sink(eng: CatalogRefreshEngine) -> None:
    eng._fact_sink = None
    await eng._emit(changed_fact(_projection()))  # must not raise


async def test_emit_calls_sync_sink(eng: CatalogRefreshEngine) -> None:
    seen: list = []
    eng._fact_sink = lambda fact: seen.append(fact)
    fact = changed_fact(_projection())
    await eng._emit(fact)
    assert seen == [fact]


async def test_emit_awaits_async_sink(eng: CatalogRefreshEngine) -> None:
    seen: list = []

    async def sink(fact) -> None:
        seen.append(fact)

    eng._fact_sink = sink
    fact = changed_fact(_projection())
    await eng._emit(fact)
    assert seen == [fact]


async def test_emit_swallows_sink_exception(eng: CatalogRefreshEngine) -> None:
    def sink(fact) -> None:
        raise RuntimeError("boom")

    eng._fact_sink = sink
    await eng._emit(changed_fact(_projection()))  # must not raise


async def test_project_skips_when_generation_not_live(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    eng._live_generations = {"p1": "stale"}  # projection generation g1 is not live
    seen: list = []
    eng._fact_sink = lambda fact: seen.append(fact)
    await eng._project(_projection())
    assert eng._projections == {}  # not stored
    assert seen == []  # not emitted


async def test_project_stores_and_emits_new_projection(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    seen: list = []
    eng._fact_sink = lambda fact: seen.append(fact)
    p = _projection(status="pending")
    await eng._project(p)
    assert eng._projections["p1"] is p
    assert len(seen) == 1
    assert seen[0].kind == "catalog_changed"


async def test_project_dedups_unchanged_projection(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    seen: list = []
    eng._fact_sink = lambda fact: seen.append(fact)
    p = _projection()
    await eng._project(p)
    await eng._project(p)  # identical -> suppressed
    assert len(seen) == 1
    assert eng._projections["p1"] == p


async def test_inspection_failure_unsupported_clears_schedule(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    eng._next_due["p1"] = 50.0
    eng._fact_sink = lambda fact: None
    await eng._inspection_failure(
        {"id": "p1", "generation": "g1"},
        SourceInspection(status="unsupported", reason="no source"),
    )
    assert "p1" not in eng._next_due  # unsupported clears the schedule
    assert "p1" not in eng._failures  # no retry scheduled


async def test_inspection_failure_error_schedules_retry(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    eng._fact_sink = lambda fact: None
    await eng._inspection_failure(
        {"id": "p1", "generation": "g1"},
        SourceInspection(status="error", reason="boom"),
    )
    assert eng._failures["p1"] == 1
    assert "p1" in eng._next_due


async def test_publish_failure_projects_and_returns(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    eng._fact_sink = lambda fact: None
    auth = types.SimpleNamespace(fingerprint="fp")
    p = await eng._publish_failure(
        {"id": "p1", "generation": "g1"}, "error", "boom", auth
    )
    assert (p.status, p.reason) == ("error", "boom")
    assert eng._projections["p1"] is p


async def test_discovery_failure_unsupported_skips_retry(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    eng._fact_sink = lambda fact: None
    p = await eng._discovery_failure(
        {"id": "p1", "generation": "g1"},
        DiscoveryResult(status="unsupported", reason="no source"),
        types.SimpleNamespace(fingerprint="fp"),
    )
    assert p.status == "unsupported"
    assert "p1" not in eng._failures


async def test_discovery_failure_error_schedules_retry(eng, monkeypatch) -> None:
    _freeze_time(monkeypatch, 0.0)
    _live(eng)
    eng._fact_sink = lambda fact: None
    await eng._discovery_failure(
        {"id": "p1", "generation": "g1"},
        DiscoveryResult(status="error", reason="boom"),
        types.SimpleNamespace(fingerprint="fp"),
    )
    assert eng._failures["p1"] == 1
    assert "p1" in eng._next_due
