#!/usr/bin/env python3
"""Unit coverage for the async orchestration layer of
model_catalog_refresh_engine.py (CatalogRefreshEngine).

The pure helpers + _emit/_project/_inspection_failure/_publish_failure/
_discovery_failure are covered by test_model_catalog_refresh_engine_helpers.py.
This file covers the remaining async orchestration: shutdown, reconcile,
_cancel_provider_tasks, _remove_projection, manual_refresh, _manual_refresh,
reconcile_provider, _reconcile_provider_locked, _refresh_singleflight,
_execute_refresh, source_changed, inspect_sources, degrade_watcher, plus the
_read_provider_cache / _write_provider_cache wrappers.

External async boundaries (codex CLI subprocess discovery, filesystem cache,
provider config) are stubbed at the module-global references the engine calls
as bare names — this is legitimate UNIT-tier coverage of the engine's own
state-machine branching. The full-stack tier covers the real wiring.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

import _test_home

_test_home.isolate("bc-test-model-catalog-engine-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import model_catalog_refresh_engine as mce  # noqa: E402
from codex_model_discovery import DiscoveryResult, SourceInspection  # noqa: E402
from model_catalog_refresh_engine import CatalogRefreshEngine  # noqa: E402
from model_catalog_refresh_state import CatalogProjection  # noqa: E402

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# fixtures + fakes
# ---------------------------------------------------------------------------

@pytest.fixture
def eng() -> CatalogRefreshEngine:
    return CatalogRefreshEngine(
        wake=lambda *a, **k: None,
        fact_sink=None,
        max_age_seconds=100.0,
        retry_base_seconds=10.0,
        retry_max_seconds=300.0,
        max_concurrency=4,
    )


def _auth(generation: str = "g1", fingerprint: str = "fp1", provider_id: str = "p1"):
    # CatalogAuthority is a complex frozen dataclass; the engine only reads
    # .fingerprint / .provider_generation / equality, so a SimpleNamespace is
    # an equivalent stand-in (two equal dicts compare ==).
    return types.SimpleNamespace(
        provider_id=provider_id,
        provider_generation=generation,
        fingerprint=fingerprint,
    )


def _snap(
    authority=None,
    models=("m1", "m2"),
    retired=(),
    last_refreshed_at=100.0,
    digest="dig1",
):
    return types.SimpleNamespace(
        authority=authority or _auth(),
        models=models,
        retired=retired,
        last_refreshed_at=last_refreshed_at,
        digest=digest,
    )


def _cache_read(status="matching", snapshot=None, stored_authority=None):
    return types.SimpleNamespace(
        status=status, snapshot=snapshot, stored_authority=stored_authority,
    )


def _inspection(status="unavailable", reason="", authority=None):
    # SourceInspection is a frozen dataclass whose __post_init__ rejects any
    # authority that isn't a real CatalogAuthority; the engine never
    # type-checks authority (only the dataclass self-validation does), so
    # bypass __init__ via __new__ and set fields with object.__setattr__.
    obj = object.__new__(SourceInspection)
    object.__setattr__(obj, "status", status)
    object.__setattr__(obj, "reason", reason)
    object.__setattr__(obj, "authority", authority)
    return obj


def _discovery(status="ok", reason="", models=(), authority=None):
    obj = object.__new__(DiscoveryResult)
    object.__setattr__(obj, "status", status)
    object.__setattr__(obj, "reason", reason)
    object.__setattr__(obj, "models", models)
    object.__setattr__(obj, "authority", authority)
    return obj


def _provider(pid="p1", gen="g1", **extra) -> dict:
    base = {"id": pid, "kind": "codex", "generation": gen}
    base.update(extra)
    return base


def _live(eng: CatalogRefreshEngine, pid="p1", gen="g1") -> None:
    eng._live_generations = {pid: gen}


_NOW = 1000.0


def _freeze_time(monkeypatch, now: float = _NOW) -> None:
    # Replace only this module's `time` reference so time.time() is deterministic.
    monkeypatch.setattr(mce, "time", types.SimpleNamespace(time=lambda: now))


def _wire(
    monkeypatch,
    *,
    inspect=None,
    discover=None,
    read=None,
    write=None,
    list_providers=None,
):
    """Stub the module-global external boundaries the engine calls.

    inspect/discover callbacks may be sync OR async; await if needed.
    """
    if inspect is not None:
        async def _inspect(pid):
            outcome = inspect(pid)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            return outcome
        monkeypatch.setattr(mce, "inspect_catalog_source", _inspect)
    if discover is not None:
        async def _discover(pid, *, expected_authority=None):
            outcome = discover(pid, expected_authority=expected_authority)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            return outcome
        monkeypatch.setattr(mce, "discover_models", _discover)
    if read is not None:
        monkeypatch.setattr(mce, "_read_provider_cache", read)
    if write is not None:
        monkeypatch.setattr(mce, "_write_provider_cache", write)
    if list_providers is not None:
        monkeypatch.setattr(
            mce, "config_store",
            types.SimpleNamespace(list_providers=list_providers),
        )


# ===========================================================================
# shutdown
# ===========================================================================

class TestShutdown:
    async def test_cancels_and_clears_inflight_and_manual(self, eng):
        async def long():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        t1 = asyncio.create_task(long())
        t2 = asyncio.create_task(long())
        eng._inflight[("p1", "g1")] = t1
        eng._manual_inflight[("p2", "g1")] = t2
        eng._reconcile_locks[("p1", "g1")] = asyncio.Lock()

        await eng.shutdown()

        assert t1.cancelled() and t2.cancelled()
        assert eng._inflight == {}
        assert eng._manual_inflight == {}
        assert eng._reconcile_locks == {}

    async def test_shutdown_no_tasks_is_noop(self, eng):
        await eng.shutdown()  # must not raise
        assert eng._inflight == {}


# ===========================================================================
# _cancel_provider_tasks
# ===========================================================================

class TestCancelProviderTasks:
    async def test_cancels_other_generations_keeps_matching(self, eng):
        async def long():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        keep = asyncio.create_task(long())
        gone = asyncio.create_task(long())
        eng._inflight[("p1", "g1")] = keep       # keep_generation g1
        eng._inflight[("p1", "g2")] = gone       # different generation -> cancel
        eng._manual_inflight[("p1", "g2")] = gone

        await eng._cancel_provider_tasks("p1", keep_generation="g1")

        assert not keep.cancelled()
        assert gone.cancelled()
        assert ("p1", "g1") in eng._inflight
        assert ("p1", "g2") not in eng._inflight
        assert eng._manual_inflight == {}

    async def test_no_matching_tasks(self, eng):
        await eng._cancel_provider_tasks("p9", keep_generation="g1")
        assert eng._inflight == {}


# ===========================================================================
# _remove_projection
# ===========================================================================

class TestRemoveProjection:
    async def test_clears_all_state_and_emits_removed_fact(self, eng, monkeypatch):
        _live(eng)
        facts = []
        eng._fact_sink = lambda f: facts.append(f)
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )
        eng._snapshots["p1"] = _snap()
        eng._authorities["p1"] = _auth()
        eng._next_due["p1"] = 50.0
        eng._failures["p1"] = 2
        eng._reconcile_locks[("p1", "g1")] = asyncio.Lock()
        eng._reconcile_locks[("p2", "g1")] = asyncio.Lock()

        await eng._remove_projection("p1")

        assert "p1" not in eng._projections
        assert "p1" not in eng._snapshots
        assert "p1" not in eng._authorities
        assert "p1" not in eng._next_due
        assert "p1" not in eng._failures
        assert ("p1", "g1") not in eng._reconcile_locks
        assert ("p2", "g1") in eng._reconcile_locks  # other provider untouched
        assert len(facts) == 1
        assert facts[0].kind == "catalog_removed"


# ===========================================================================
# reconcile_provider / _reconcile_provider_locked
# ===========================================================================

class TestReconcileProvider:
    async def test_fresh_provider_unavailable_inspection(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        _wire(monkeypatch, inspect=lambda pid: _inspection(
            status="unsupported", reason="no source",
        ))
        await eng.reconcile_provider(_provider(), force=False)
        snap = eng.snapshot("p1")
        assert snap.status == "unsupported"
        assert snap.reason == "no source"
        # unsupported clears schedule (no next_due)
        assert "p1" not in eng._next_due

    async def test_inspection_available_cache_read_raises(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None

        def _read(pid, auth):
            raise OSError("disk gone")
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=_auth()),
            read=_read,
        )
        await eng.reconcile_provider(_provider(), force=False)
        snap = eng.snapshot("p1")
        assert snap.status == "error"
        assert snap.reason == "cache_unavailable"
        assert eng._failures["p1"] == 1  # retry scheduled

    async def test_fresh_matching_snapshot_not_force_current(self, eng, monkeypatch):
        _live(eng)
        _freeze_time(monkeypatch, _NOW)
        eng._fact_sink = lambda f: None
        snap = _snap(last_refreshed_at=950.0)  # fresh: due_at=1050 > now=1000
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap.authority),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snap),
        )
        await eng.reconcile_provider(_provider(), force=False)
        snap_proj = eng.snapshot("p1")
        assert snap_proj.status == "current"
        assert snap_proj.models_current is True
        assert eng._snapshots["p1"] is snap
        assert "p1" not in eng._failures

    async def test_force_overrides_fresh_snapshot_triggers_refresh(self, eng, monkeypatch):
        _live(eng)
        _freeze_time(monkeypatch, _NOW)
        eng._fact_sink = lambda f: None
        snap = _snap(last_refreshed_at=950.0)  # fresh, but force=True must still refresh
        refreshed = {"done": False}
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap.authority),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snap),
            write=lambda pid, result, retired, at: refreshed.__setitem__("done", True),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1", "m2"), authority=snap.authority,
            ),
        )
        await eng.reconcile_provider(_provider(), force=True)
        # force=True bypasses the fresh-snapshot early-return -> _refresh_singleflight runs
        assert refreshed["done"] is True

    async def test_stale_snapshot_triggers_refresh(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        # snapshot older than max_age -> not fresh
        old_snap = _snap(last_refreshed_at=-1000.0)
        written = []

        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=old_snap.authority),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=old_snap),
            write=lambda pid, result, retired, at: written.append((pid, retired, at)),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=old_snap.authority,
            ),
        )
        await eng.reconcile_provider(_provider(), force=False)
        # not fresh -> projected stale, then refresh ran
        assert eng.snapshot("p1").status in ("stale", "refreshing", "current")
        assert len(written) == 1  # refresh wrote cache

    async def test_read_status_source_changed_triggers_refresh(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        snap = _snap(last_refreshed_at=0.0)
        written = []
        read_calls = {"n": 0}

        def read(pid, auth):
            read_calls["n"] += 1
            # 1st (reconcile) read: source_changed -> not fresh -> refresh.
            # post-write read: matching -> refresh succeeds.
            if read_calls["n"] == 1:
                return _cache_read(status="source_changed", snapshot=snap)
            return _cache_read(status="matching", snapshot=snap)
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap.authority),
            read=read,
            write=lambda pid, result, retired, at: written.append(at),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=snap.authority,
            ),
        )
        await eng.reconcile_provider(_provider(), force=False)
        # source_changed read -> snapshot_is_fresh False -> refresh ran (wrote cache)
        assert len(written) == 1
        # refresh succeeded -> final projection is current
        assert eng.snapshot("p1").status == "current"

    async def test_snapshot_authority_generation_mismatch_not_stored(self, eng, monkeypatch):
        _live(eng)
        _freeze_time(monkeypatch, _NOW)
        eng._fact_sink = lambda f: None
        # snapshot.authority.provider_generation differs from provider generation,
        # but snapshot is fresh -> line-319 store is skipped yet the fresh early
        # return still fires (no refresh), so the mismatch-skip is observable.
        snap = _snap(authority=_auth(generation="OTHER"), last_refreshed_at=950.0)
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=_auth()),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snap),
        )
        await eng.reconcile_provider(_provider(), force=False)
        assert "p1" not in eng._snapshots  # generation mismatch -> not stored
        assert eng.snapshot("p1").status == "current"  # fresh early-return, no refresh

    async def test_not_live_after_inspection_returns(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()

        def inspect(pid):
            # provider becomes stale mid-flight
            eng._live_generations = {}  # no longer live
            return _inspection(status="available", reason="", authority=auth)
        _wire(
            monkeypatch,
            inspect=inspect,
            read=lambda pid, a: _cache_read(snapshot=_snap()),
        )
        await eng.reconcile_provider(_provider(), force=False)
        # returned right after inspect (no authority stored, no refresh)
        assert "p1" not in eng._authorities
        assert eng.snapshot("p1").status == "pending"

    async def test_not_live_after_read_returns(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        snap = _snap(last_refreshed_at=-1000.0)
        calls = {"read": 0}

        def read(pid, auth):
            calls["read"] += 1
            eng._live_generations = {}  # go stale
            return _cache_read(status="matching", snapshot=snap)
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap.authority),
            read=read,
        )
        await eng.reconcile_provider(_provider(), force=False)
        assert calls["read"] == 1
        # not live after read -> no refresh, no next_due
        assert "p1" not in eng._next_due

    async def test_generation_change_resets_prior_projection(self, eng, monkeypatch):
        _live(eng, gen="g2")
        eng._fact_sink = lambda f: None
        # pre-existing projection at old generation
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )
        eng._reconcile_locks[("p1", "g1")] = asyncio.Lock()
        _freeze_time(monkeypatch, _NOW)
        snap = _snap(authority=_auth(generation="g2"), last_refreshed_at=950.0)
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap.authority),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snap),
        )
        await eng.reconcile_provider(_provider(gen="g2"), force=False)
        # locks pruned to new generation, pending projected
        assert ("p1", "g1") not in eng._reconcile_locks
        assert ("p1", "g2") in eng._reconcile_locks
        assert eng.snapshot("p1").provider_generation == "g2"


# ===========================================================================
# _execute_refresh (via _refresh_singleflight)
# ===========================================================================

class TestExecuteRefresh:
    async def test_success_path(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        final_snap = _snap(authority=auth, last_refreshed_at=5.0)
        written = []

        def inspect(pid):
            # second call (post-write re-inspect) returns same authority
            return _inspection(status="available", reason="", authority=auth)
        _wire(
            monkeypatch,
            inspect=inspect,
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1", "m2"), authority=auth,
            ),
            write=lambda pid, result, retired, at: written.append((retired, at)),
            read=lambda pid, a: _cache_read(status="matching", snapshot=final_snap),
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "current"
        assert result.models_current is True
        assert eng._snapshots["p1"] is final_snap
        assert eng._authorities["p1"] is auth
        assert "p1" not in eng._failures
        assert "p1" in eng._next_due
        assert written and written[0][0] == []  # no retired

    async def test_discovery_failure_error(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="error", reason="boom", authority=auth,
            ),
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "error"
        assert eng._failures["p1"] == 1

    async def test_discovery_failure_unsupported(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="unsupported", reason="no source", authority=auth,
            ),
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "unsupported"
        assert "p1" not in eng._failures  # unsupported clears schedule
        assert "p1" not in eng._next_due

    async def test_write_raises(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: (_ for _ in ()).throw(OSError("write fail")),
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "error"
        assert result.reason == "cache_commit_unavailable"
        assert eng._failures["p1"] == 1

    async def test_post_write_authority_mismatch(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        other = _auth(fingerprint="other")
        calls = {"n": 0}

        def inspect(pid):
            calls["n"] += 1
            if calls["n"] == 1:
                return _inspection(status="available", reason="", authority=other)
            return _inspection(status="available", reason="", authority=other)
        _wire(
            monkeypatch,
            inspect=inspect,
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "stale"
        assert result.reason == "authority_changed"
        assert eng._failures["p1"] == 1

    async def test_post_write_inspect_unavailable(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        # _refresh_singleflight -> _execute_refresh inspects ONCE (post-write).
        # That single re-inspect returning unavailable -> authority_changed path.
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="unavailable", reason="gone"),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.reason == "authority_changed"

    async def test_post_write_read_raises(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        # single post-write read raises -> cache_unavailable
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: (_ for _ in ()).throw(OSError("read fail")),
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "error"
        assert result.reason == "cache_unavailable"

    async def test_post_write_read_status_not_matching(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        # single post-write read returns missing -> cache_commit_unavailable
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: _cache_read(status="missing", snapshot=None),
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "error"
        assert result.reason == "cache_commit_unavailable"

    async def test_not_live_after_discover(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()

        async def discover(pid, *, expected_authority=None):
            eng._live_generations = {}  # go stale
            return _discovery(status="ok", reason="", models=("m1",), authority=auth)
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=discover,
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "stale"
        assert result.reason == "provider_changed"

    async def test_not_live_after_write(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()

        def write(pid, result, retired, at):
            eng._live_generations = {}  # go stale mid-write
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=write,
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "stale"
        assert result.reason == "provider_changed"

    async def test_not_live_after_current_inspect(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()

        def inspect(pid):
            eng._live_generations = {}  # go stale at post-write re-inspect
            return _inspection(status="available", reason="", authority=auth)
        _wire(
            monkeypatch,
            inspect=inspect,
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "stale"
        assert result.reason == "provider_changed"

    async def test_not_live_after_final_read(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        final_snap = _snap(authority=auth)

        def read(pid, a):
            eng._live_generations = {}  # go stale at final read
            return _cache_read(status="matching", snapshot=final_snap)
        _wire(
            monkeypatch,
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
            read=read,
        )
        result = await eng._refresh_singleflight(_provider(), auth)
        assert result.status == "stale"
        assert result.reason == "provider_changed"
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        discover_calls = {"n": 0}

        async def discover(pid, *, expected_authority=None):
            discover_calls["n"] += 1
            await asyncio.sleep(0.01)
            return _discovery(status="ok", reason="", models=("m1",), authority=auth)

        calls = {"n": 0}

        def inspect(pid):
            calls["n"] += 1
            return _inspection(status="available", reason="", authority=auth)
        _wire(
            monkeypatch,
            inspect=inspect,
            discover=discover,
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: _cache_read(snapshot=_snap(authority=auth)),
        )
        # kick off two concurrent singleflight calls for the same key
        r1, r2 = await asyncio.gather(
            eng._refresh_singleflight(_provider(), auth),
            eng._refresh_singleflight(_provider(), auth),
        )
        assert r1 is r2  # same task result
        assert discover_calls["n"] == 1  # singleflighted


# ===========================================================================
# reconcile (full)
# ===========================================================================

class TestReconcile:
    async def test_removes_retired_providers_and_reconciles(self, eng, monkeypatch):
        eng._fact_sink = lambda f: None
        eng._live_generations = {"p1": "g1"}  # prior live
        # stale projection for p1 (will be removed because not in new state)
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="gold", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )
        eng._snapshots["p1"] = _snap()

        state = {"providers": [_provider(pid="p2", gen="g2")]}
        _freeze_time(monkeypatch, _NOW)
        _live_after = {"p2": "g2"}
        # inspect for p2 -> available; fresh matching snapshot -> current, no refresh
        snap2 = _snap(authority=_auth(generation="g2", fingerprint="fp2"), last_refreshed_at=950.0)
        _wire(
            monkeypatch,
            list_providers=lambda: state,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap2.authority),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snap2),
        )
        await eng.reconcile(state)
        # p1 retired (removed), p2 live
        assert "p1" not in eng._projections
        assert "p1" not in eng._snapshots
        assert eng._live_generations == _live_after
        assert eng.snapshot("p2") is not None

    async def test_cancels_changed_generation_tasks(self, eng, monkeypatch):
        eng._fact_sink = lambda f: None
        _freeze_time(monkeypatch, _NOW)
        # prior generation g1 task inflight; new state still has p1 but gen g2
        async def long():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(long())
        eng._inflight[("p1", "g1")] = task
        eng._live_generations = {"p1": "g1"}
        state = {"providers": [_provider(gen="g2")]}
        snap = _snap(authority=_auth(generation="g2"), last_refreshed_at=950.0)
        _wire(
            monkeypatch,
            list_providers=lambda: state,
            inspect=lambda pid: _inspection(status="available", reason="", authority=snap.authority),
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snap),
        )
        await eng.reconcile(state)
        assert task.cancelled()
        assert ("p1", "g1") not in eng._inflight

    async def test_reconcile_loop_continues_across_multiple_priors(self, eng, monkeypatch):
        # 143->142: the previous_live loop processes more than one entry.
        eng._fact_sink = lambda f: None
        _freeze_time(monkeypatch, _NOW)
        eng._live_generations = {"p1": "g1", "p2": "g2"}  # two prior providers
        state = {
            "providers": [
                _provider(pid="p1", gen="g1"),
                _provider(pid="p2", gen="g2"),
            ]
        }
        snap = _snap(authority=_auth(), last_refreshed_at=950.0)
        snap2 = _snap(authority=_auth(generation="g2", fingerprint="fp2"), last_refreshed_at=950.0)
        snaps = {"p1": snap, "p2": snap2}

        def inspect(pid):
            return _inspection(status="available", reason="", authority=snaps[pid].authority)
        _wire(
            monkeypatch,
            list_providers=lambda: state,
            inspect=inspect,
            read=lambda pid, auth: _cache_read(status="matching", snapshot=snaps[pid]),
        )
        await eng.reconcile(state)
        # both providers reconciled (loop ran over both prior entries)
        assert eng.snapshot("p1") is not None
        assert eng.snapshot("p2") is not None


# ===========================================================================
# manual_refresh / _manual_refresh
# ===========================================================================

class TestManualRefresh:
    async def test_provider_unavailable_raises(self, eng):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await eng.manual_refresh("p1")

    async def test_returns_existing_projection_via_refresh(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )
        auth = _auth()
        snap = _snap(authority=auth, last_refreshed_at=5.0)
        # force=True always refreshes -> wire the full execute_refresh success path
        _wire(
            monkeypatch,
            list_providers=lambda: {"providers": [_provider()]},
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=lambda pid, *, expected_authority=None: _discovery(
                status="ok", reason="", models=("m1",), authority=auth,
            ),
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: _cache_read(status="matching", snapshot=snap),
        )
        result = await eng.manual_refresh("p1")
        assert result.provider_id == "p1"
        assert result.status == "current"
        # manual task cleaned up after completion
        assert ("p1", "g1") not in eng._manual_inflight

    async def test_reuses_inflight_manual_task(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )
        auth = _auth()
        snap = _snap(authority=auth, last_refreshed_at=5.0)
        discover_calls = {"n": 0}

        async def discover(pid, *, expected_authority=None):
            discover_calls["n"] += 1
            await asyncio.sleep(0.01)
            return _discovery(status="ok", reason="", models=("m1",), authority=auth)
        _wire(
            monkeypatch,
            list_providers=lambda: {"providers": [_provider()]},
            inspect=lambda pid: _inspection(status="available", reason="", authority=auth),
            discover=discover,
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: _cache_read(status="matching", snapshot=snap),
        )
        r1, r2 = await asyncio.gather(
            eng.manual_refresh("p1"),
            eng.manual_refresh("p1"),
        )
        # both completed; singleflighted -> discover ran once
        assert r1.provider_id == "p1" and r2.provider_id == "p1"
        assert discover_calls["n"] == 1

    async def test_cancelled_same_generation_reraises(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )

        async def discover(pid, *, expected_authority=None):
            await asyncio.sleep(100)  # block so the outer await can be cancelled
            return _discovery(status="ok", reason="", models=("m1",), authority=_auth())
        _wire(
            monkeypatch,
            list_providers=lambda: {"providers": [_provider()]},
            inspect=lambda pid: _inspection(status="available", reason="", authority=_auth()),
            discover=discover,
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: _cache_read(snapshot=_snap()),
        )
        outer = asyncio.create_task(eng.manual_refresh("p1"))
        await asyncio.sleep(0.02)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

    async def test_cancelled_changed_generation_raises_provider_changed(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=(), models_current=False, retired=(), last_refreshed_at=0.0,
            reason="", authority_fingerprint="",
        )

        async def discover(pid, *, expected_authority=None):
            await asyncio.sleep(100)
            return _discovery(status="ok", reason="", models=("m1",), authority=_auth())
        _wire(
            monkeypatch,
            list_providers=lambda: {"providers": [_provider()]},
            inspect=lambda pid: _inspection(status="available", reason="", authority=_auth()),
            discover=discover,
            write=lambda pid, result, retired, at: None,
            read=lambda pid, a: _cache_read(snapshot=_snap()),
        )
        outer = asyncio.create_task(eng.manual_refresh("p1"))
        await asyncio.sleep(0.02)
        eng._live_generations = {"p1": "g-changed"}  # generation changed
        outer.cancel()
        with pytest.raises(RuntimeError, match="provider changed"):
            await outer


class TestManualRefreshInternal:
    async def test_provider_not_in_state_raises(self, eng, monkeypatch):
        _wire(monkeypatch, list_providers=lambda: {"providers": []})
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await eng._manual_refresh("p1")

    async def test_generation_changed_raises(self, eng, monkeypatch):
        _live(eng, gen="g2")
        _wire(monkeypatch, list_providers=lambda: {"providers": [_provider(gen="g1")]})
        with pytest.raises(RuntimeError, match="provider changed"):
            await eng._manual_refresh("p1")

    # NOTE: _manual_refresh line 248 (`raise RuntimeError("catalog projection
    # unavailable")`) is defensive dead code: reconcile_provider always stores
    # a pending projection for a live, matching-generation provider before
    # returning, so self._projections[provider_id] cannot be None afterwards.
    # Excluded rather than faked.


# ===========================================================================
# source_changed / inspect_sources / degrade_watcher
# ===========================================================================

class TestSourceChanged:
    async def test_not_live_is_noop(self, eng, monkeypatch):
        _live(eng, gen="other")
        eng._fact_sink = lambda f: None
        called = {"n": 0}

        def inspect(pid):
            called["n"] += 1
            return _inspection(status="available", reason="", authority=_auth())
        _wire(monkeypatch, inspect=inspect)
        await eng.source_changed(_provider(), _inspection(status="available", reason="", authority=_auth()))
        assert called["n"] == 0  # short-circuited before any inspection

    async def test_unavailable_inspection_routes_to_failure(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        await eng.source_changed(
            _provider(),
            _inspection(status="error", reason="boom"),
        )
        assert eng.snapshot("p1").status == "error"
        assert eng._failures["p1"] == 1

    async def test_available_projects_stale_source_changed(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        auth = _auth()
        await eng.source_changed(
            _provider(),
            _inspection(status="available", reason="", authority=auth),
        )
        snap = eng.snapshot("p1")
        assert snap.status == "stale"
        assert snap.reason == "source_changed"
        assert eng._authorities["p1"] is auth


class TestInspectSources:
    async def test_gathers_inspections(self, eng, monkeypatch):
        seen = []

        def inspect(pid):
            seen.append(pid)
            return _inspection(status="available", reason="", authority=_auth(provider_id=pid))
        _wire(monkeypatch, inspect=inspect)
        providers = [_provider(pid="p1"), _provider(pid="p2")]
        results = await eng.inspect_sources(providers)
        assert sorted(seen) == ["p1", "p2"]
        assert len(results) == 2


class TestDegradeWatcher:
    async def test_projects_error_for_all(self, eng, monkeypatch):
        _live(eng)
        eng._fact_sink = lambda f: None
        eng._projections["p1"] = CatalogProjection(
            provider_id="p1", provider_generation="g1", status="current",
            models=("m1",), models_current=True, retired=(), last_refreshed_at=5.0,
            reason="", authority_fingerprint="fp",
        )
        await eng.degrade_watcher()
        snap = eng.snapshot("p1")
        assert snap.status == "error"
        assert snap.models_current is False
        assert snap.reason == "watcher_unavailable"
        assert snap.models == ("m1",)  # preserved


# ===========================================================================
# module-level cache wrappers
# ===========================================================================

class TestProviderCacheWrappers:
    def test_read_provider_cache_delegates(self, monkeypatch):
        captured = {}

        def fake_read_cache(path, *, expected_authority):
            captured["path"] = path
            captured["auth"] = expected_authority
            return "READ-RESULT"

        monkeypatch.setattr(mce, "read_catalog_cache", fake_read_cache)
        auth = _auth()
        out = mce._read_provider_cache("p1", auth)
        assert out == "READ-RESULT"
        assert captured["auth"] is auth
        # path is the hashed catalog_cache_path(provider_id) under model_catalog
        assert "model_catalog" in str(captured["path"])

    def test_write_provider_cache_delegates(self, monkeypatch):
        captured = {}

        def fake_write(path, result, *, retired, refreshed_at):
            captured.update(path=path, result=result, retired=retired, at=refreshed_at)

        monkeypatch.setattr(mce, "write_discovery_cache", fake_write)
        result = _discovery(status="ok", reason="", models=("m1",), authority=_auth())
        mce._write_provider_cache("p1", result, retired=[], refreshed_at=42.0)
        assert captured["result"] is result
        assert captured["retired"] == []
        assert captured["at"] == 42.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
