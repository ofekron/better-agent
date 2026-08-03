"""Unit coverage for model_catalog_refresh_state.py — catalog projection facts.

Pure deterministic logic with no I/O and no threads: builds the
catalog-changed / catalog-removed facts emitted after a refresh, and computes
which models are newly retired (or re-promoted) after a successful fetch.
Drives every branch directly through the public functions.

retired_after_success only reads ``previous.models`` and ``previous.retired``,
so a minimal duck-typed stand-in is a faithful input — it is a value object,
not a behavior mock.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from model_catalog_cache import MAX_RETIRED, RetiredModel
from model_catalog_refresh_state import (
    CatalogProjection,
    changed_fact,
    removed_fact,
    retired_after_success,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _projection(**overrides) -> CatalogProjection:
    fields = dict(
        provider_id="codex",
        provider_generation="gen-1",
        status="current",
        models=("alpha", "beta"),
        models_current=True,
        retired=(),
        last_refreshed_at=10.0,
        reason="ok",
        authority_fingerprint="fp-sha",
    )
    fields.update(overrides)
    return CatalogProjection(**fields)


def _previous(models=(), retired=()) -> SimpleNamespace:
    return SimpleNamespace(models=tuple(models), retired=tuple(retired))


# ── CatalogProjection.to_dict ───────────────────────────────────


def test_to_dict_serializes_models_and_retired():
    projection = _projection(retired=(RetiredModel("old", 1.0),))
    payload = projection.to_dict()
    assert payload == {
        "provider_id": "codex",
        "provider_generation": "gen-1",
        "status": "current",
        "models": ["alpha", "beta"],
        "models_current": True,
        "retired": [{"id": "old", "first_absent_at": 1.0}],
        "last_refreshed_at": 10.0,
        "reason": "ok",
        "authority_fingerprint": "fp-sha",
    }


# ── changed_fact ────────────────────────────────────────────────


def test_changed_fact_without_snapshot():
    projection = _projection()
    fact = changed_fact(projection)
    assert fact.kind == "catalog_changed"
    assert fact.provider_id == "codex"
    assert fact.provider_generation == "gen-1"
    assert fact.projection is projection
    assert fact.snapshot is None
    assert _HEX64.fullmatch(fact.idempotency_key)


def test_changed_fact_with_snapshot_carries_digest_into_key():
    projection = _projection()
    snapshot = SimpleNamespace(digest="deadbeef")
    fact = changed_fact(projection, snapshot=snapshot)
    assert fact.snapshot is snapshot
    # The snapshot digest is folded into the idempotency payload, so the key
    # must differ from the no-snapshot fact for the same projection.
    assert fact.idempotency_key != changed_fact(projection).idempotency_key
    assert _HEX64.fullmatch(fact.idempotency_key)


def test_changed_fact_is_deterministic_for_same_inputs():
    projection = _projection()
    assert changed_fact(projection).idempotency_key == changed_fact(
        projection,
    ).idempotency_key


# ── removed_fact ────────────────────────────────────────────────


def test_removed_fact_shape():
    fact = removed_fact("codex", "gen-1")
    assert fact.kind == "catalog_removed"
    assert fact.provider_id == "codex"
    assert fact.provider_generation == "gen-1"
    assert fact.projection is None
    assert fact.snapshot is None
    assert _HEX64.fullmatch(fact.idempotency_key)


def test_removed_fact_distinct_per_generation():
    assert removed_fact("codex", "gen-1").idempotency_key == removed_fact(
        "codex", "gen-1",
    ).idempotency_key
    assert removed_fact("codex", "gen-1").idempotency_key != removed_fact(
        "codex", "gen-2",
    ).idempotency_key


# ── retired_after_success ───────────────────────────────────────


def test_no_previous_returns_empty():
    assert retired_after_success(None, ("alpha",), absent_at=5.0) == ()


def test_no_previous_with_no_active_models_returns_empty():
    assert retired_after_success(None, (), absent_at=5.0) == ()


def test_keeps_existing_retired_record_when_still_absent():
    previous = _previous(retired=(RetiredModel("old", 1.0),))
    # "old" is absent from the freshly-fetched models -> retirement is kept,
    # preserving its original first_absent_at.
    result = retired_after_success(previous, ("alpha",), absent_at=5.0)
    assert result == (RetiredModel("old", 1.0),)


def test_drops_retired_record_when_model_reappeared():
    previous = _previous(retired=(RetiredModel("old", 1.0),))
    # "old" is back in the catalog -> it is no longer retired.
    assert retired_after_success(previous, ("old",), absent_at=5.0) == ()


def test_retires_newly_absent_models_ordered_by_id():
    previous = _previous(models=("alpha", "beta", "gamma"))
    # Only "alpha" remains -> beta and gamma are newly absent at absent_at.
    result = retired_after_success(previous, ("alpha",), absent_at=5.0)
    assert result == (RetiredModel("beta", 5.0), RetiredModel("gamma", 5.0))


def test_does_not_overwrite_existing_retired_absent_model():
    # "beta" was already retired at t=1.0 and is still absent -> it must NOT be
    # re-stamped with the newer absent_at.
    previous = _previous(models=("alpha", "beta"), retired=(RetiredModel("beta", 1.0),))
    result = retired_after_success(previous, ("alpha",), absent_at=5.0)
    assert result == (RetiredModel("beta", 1.0),)


def test_orders_newest_absence_first_then_id():
    # gamma was retired long ago (t=100); alpha/beta disappear now (t=5).
    # Sort is (-first_absent_at, id): gamma(100) first, then alpha/beta(5) by id.
    previous = _previous(
        models=("alpha", "beta", "gamma"),
        retired=(RetiredModel("gamma", 100.0),),
    )
    result = retired_after_success(previous, (), absent_at=5.0)
    assert result == (
        RetiredModel("gamma", 100.0),
        RetiredModel("alpha", 5.0),
        RetiredModel("beta", 5.0),
    )


def test_caps_at_max_retired():
    # More newly-absent models than MAX_RETIRED -> truncated, newest absences first.
    models = tuple(f"m{i:03d}" for i in range(MAX_RETIRED + 1))
    previous = _previous(models=models)
    result = retired_after_success(previous, (), absent_at=9.0)
    assert len(result) == MAX_RETIRED
    # All retained records share absent_at (9.0), so ordering is by id ascending
    # after the (negated) timestamp tie-break. The lexicographically largest id
    # is the one dropped.
    dropped = f"m{MAX_RETIRED:03d}"
    assert all(record.id != dropped for record in result)
    assert all(record.first_absent_at == 9.0 for record in result)
