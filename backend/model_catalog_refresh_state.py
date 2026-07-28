from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from codex_execution_common import canonical_json
from model_catalog_cache import MAX_RETIRED, CatalogSnapshot, RetiredModel


CatalogStatus = Literal[
    "pending",
    "refreshing",
    "current",
    "stale",
    "error",
    "unsupported",
    "unavailable",
]


@dataclass(frozen=True)
class CatalogProjection:
    provider_id: str
    provider_generation: str
    status: CatalogStatus
    models: tuple[str, ...]
    models_current: bool
    retired: tuple[RetiredModel, ...]
    reason: str
    authority_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_generation": self.provider_generation,
            "status": self.status,
            "models": list(self.models),
            "models_current": self.models_current,
            "retired": [record.to_dict() for record in self.retired],
            "reason": self.reason,
            "authority_fingerprint": self.authority_fingerprint,
        }


@dataclass(frozen=True)
class CatalogChangedFact:
    kind: Literal["catalog_changed", "catalog_removed"]
    provider_id: str
    projection: CatalogProjection | None
    idempotency_key: str


def changed_fact(projection: CatalogProjection) -> CatalogChangedFact:
    payload = projection.to_dict()
    return CatalogChangedFact(
        kind="catalog_changed",
        provider_id=projection.provider_id,
        projection=projection,
        idempotency_key=hashlib.sha256(canonical_json(payload)).hexdigest(),
    )


def removed_fact(provider_id: str, generation: str) -> CatalogChangedFact:
    payload = {
        "kind": "catalog_removed",
        "provider_id": provider_id,
        "provider_generation": generation,
    }
    return CatalogChangedFact(
        kind="catalog_removed",
        provider_id=provider_id,
        projection=None,
        idempotency_key=hashlib.sha256(canonical_json(payload)).hexdigest(),
    )


def retired_after_success(
    previous: CatalogSnapshot | None,
    models: Sequence[str],
    *,
    absent_at: float,
) -> tuple[RetiredModel, ...]:
    active = set(models)
    records = {
        record.id: record
        for record in (previous.retired if previous is not None else ())
        if record.id not in active
    }
    if previous is not None:
        for model in previous.models:
            if model not in active and model not in records:
                records[model] = RetiredModel(
                    id=model,
                    first_absent_at=absent_at,
                )
    newest = sorted(
        records.values(),
        key=lambda record: (-record.first_absent_at, record.id),
    )
    return tuple(newest[:MAX_RETIRED])
