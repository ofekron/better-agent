from __future__ import annotations

import threading

from model_catalog_refresh_state import CatalogChangedFact, CatalogProjection


_lock = threading.Lock()
_projections: dict[str, CatalogProjection] = {}


def apply_fact(fact: CatalogChangedFact) -> None:
    with _lock:
        if fact.kind == "catalog_removed":
            _projections.pop(fact.provider_id, None)
            return
        projection = fact.projection
        if projection is not None:
            _projections[fact.provider_id] = projection


def snapshot(
    provider_id: str,
    provider_generation: str,
) -> CatalogProjection | None:
    with _lock:
        projection = _projections.get(provider_id)
    if (
        projection is None
        or projection.provider_generation != provider_generation
    ):
        return None
    return projection
