"""Unit coverage for model_catalog_read_projection.py.

The in-memory read projection that ``model_catalog_refresh`` builds from
catalog-changed / catalog-removed facts and that ``models`` /
``codex_headless_execution`` / ``model_execution_admission`` read at turn time.

Pure in-memory logic guarded by a module lock. The module keeps a
process-local ``_states`` dict, so each test resets it through an autouse
fixture — that reset is a faithful "fresh process" setup, not a behavior mock.
``CatalogSnapshot`` is only carried opaquely (stored and returned, never
dereferenced), so a duck-typed stand-in is a faithful value object, matching
the idiom in ``test_model_catalog_refresh_state_unit.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from model_catalog_cache import RetiredModel
from model_catalog_read_projection import (
    CatalogRuntimeSnapshot,
    apply_fact,
    runtime_snapshot,
    snapshot,
)
from model_catalog_refresh_state import (
    CatalogChangedFact,
    CatalogProjection,
    changed_fact,
    removed_fact,
)
import model_catalog_read_projection as mcr


def _projection(provider_id="codex", generation="gen-1", **overrides) -> CatalogProjection:
    fields = dict(
        provider_id=provider_id,
        provider_generation=generation,
        status="current",
        models=("alpha",),
        models_current=True,
        retired=(),
        last_refreshed_at=10.0,
        reason="ok",
        authority_fingerprint="fp",
    )
    fields.update(overrides)
    return CatalogProjection(**fields)


@pytest.fixture(autouse=True)
def _isolate_states():
    # Each test starts from an empty projection store (fresh-process semantics).
    mcr._states.clear()
    yield
    mcr._states.clear()


# ── apply_fact: catalog_changed ─────────────────────────────────


def test_changed_fact_stores_projection_and_snapshot():
    projection = _projection()
    snap = SimpleNamespace(digest="abc")
    apply_fact(changed_fact(projection, snapshot=snap))
    state = mcr._states["codex"]
    assert isinstance(state, CatalogRuntimeSnapshot)
    assert state.projection is projection
    assert state.snapshot is snap


def test_changed_fact_without_snapshot_stores_none_snapshot():
    apply_fact(changed_fact(_projection()))
    assert mcr._states["codex"].snapshot is None


def test_changed_fact_replaces_prior_projection_for_same_provider():
    apply_fact(changed_fact(_projection(models=("alpha",), last_refreshed_at=1.0)))
    apply_fact(changed_fact(_projection(models=("alpha", "beta"), last_refreshed_at=2.0)))
    assert mcr._states["codex"].projection.models == ("alpha", "beta")
    assert mcr._states["codex"].projection.last_refreshed_at == 2.0


def test_changed_fact_routes_per_provider_id():
    apply_fact(changed_fact(_projection(provider_id="codex")))
    apply_fact(changed_fact(_projection(provider_id="agy")))
    assert set(mcr._states) == {"codex", "agy"}


def test_changed_fact_with_null_projection_is_noop():
    # CatalogChangedFact.projection is typed ``CatalogProjection | None``; the
    # factories never emit None, but the guard must keep a None projection from
    # being stored as state. Direct construction exercises that contract.
    fact = CatalogChangedFact(
        kind="catalog_changed",
        provider_id="codex",
        provider_generation="gen-1",
        projection=None,
        snapshot=None,
        idempotency_key="x",
    )
    apply_fact(fact)
    assert "codex" not in mcr._states


# ── apply_fact: catalog_removed ─────────────────────────────────


def test_removed_fact_clears_matching_generation():
    apply_fact(changed_fact(_projection(provider_id="codex", generation="gen-1")))
    apply_fact(removed_fact("codex", "gen-1"))
    assert "codex" not in mcr._states


def test_removed_fact_noop_when_provider_unknown():
    # No prior state → nothing to remove, must not raise.
    apply_fact(removed_fact("codex", "gen-1"))
    assert mcr._states == {}


def test_removed_fact_keeps_state_when_generation_mismatched():
    # A removal targets one generation; a newer-generation projection must
    # survive a stale removal fact rather than being wiped.
    apply_fact(changed_fact(_projection(provider_id="codex", generation="gen-2")))
    apply_fact(removed_fact("codex", "gen-1"))
    assert "codex" in mcr._states
    assert mcr._states["codex"].projection.provider_generation == "gen-2"


# ── snapshot / runtime_snapshot reads ───────────────────────────


def test_snapshot_returns_projection_on_match():
    projection = _projection(provider_id="codex", generation="gen-1")
    apply_fact(changed_fact(projection))
    assert snapshot("codex", "gen-1") is projection


def test_snapshot_returns_none_when_provider_absent():
    assert snapshot("missing", "gen-1") is None


def test_snapshot_returns_none_on_generation_mismatch():
    apply_fact(changed_fact(_projection(provider_id="codex", generation="gen-1")))
    assert snapshot("codex", "gen-9") is None


def test_runtime_snapshot_returns_full_state_on_match():
    snap = SimpleNamespace(digest="d")
    apply_fact(changed_fact(_projection(provider_id="codex", generation="gen-1"), snapshot=snap))
    state = runtime_snapshot("codex", "gen-1")
    assert isinstance(state, CatalogRuntimeSnapshot)
    assert state.snapshot is snap
    assert state.projection.provider_generation == "gen-1"


def test_runtime_snapshot_returns_none_when_absent():
    assert runtime_snapshot("missing", "gen-1") is None


def test_runtime_snapshot_returns_none_on_generation_mismatch():
    apply_fact(changed_fact(_projection(provider_id="codex", generation="gen-1")))
    assert runtime_snapshot("codex", "gen-2") is None


def test_retired_records_round_trip_through_store():
    # The store carries the projection verbatim, so retired-model records
    # survive a store/round-trip unchanged.
    retired = (RetiredModel("old", 1.0), RetiredModel("older", 0.5))
    projection = _projection(retired=retired)
    apply_fact(changed_fact(projection))
    assert snapshot("codex", "gen-1").retired == retired
