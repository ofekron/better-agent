from __future__ import annotations

import threading
from dataclasses import dataclass

from model_catalog_cache import CatalogSnapshot
from model_catalog_refresh_state import CatalogChangedFact, CatalogProjection


@dataclass(frozen=True)
class CatalogRuntimeSnapshot:
    projection: CatalogProjection
    snapshot: CatalogSnapshot | None


_lock = threading.Lock()
_states: dict[str, CatalogRuntimeSnapshot] = {}


def apply_fact(fact: CatalogChangedFact) -> None:
    with _lock:
        if fact.kind == "catalog_removed":
            current = _states.get(fact.provider_id)
            if (
                current is not None
                and current.projection.provider_generation
                == fact.provider_generation
            ):
                _states.pop(fact.provider_id, None)
            return
        projection = fact.projection
        if projection is not None:
            _states[fact.provider_id] = CatalogRuntimeSnapshot(
                projection=projection,
                snapshot=fact.snapshot,
            )


def snapshot(
    provider_id: str,
    provider_generation: str,
) -> CatalogProjection | None:
    state = runtime_snapshot(provider_id, provider_generation)
    return state.projection if state is not None else None


def runtime_snapshot(
    provider_id: str,
    provider_generation: str,
) -> CatalogRuntimeSnapshot | None:
    with _lock:
        state = _states.get(provider_id)
    if (
        state is None
        or state.projection.provider_generation != provider_generation
    ):
        return None
    return state
