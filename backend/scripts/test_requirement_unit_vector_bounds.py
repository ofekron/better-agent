from __future__ import annotations

import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requirement_context as context  # noqa: E402
import requirements_api  # noqa: E402
from capability_api import _RequirementsUnitVectorPayload  # noqa: E402
from requirements_vector_contract import validate_unit_vector_bounds  # noqa: E402


def _write_index(path: Path) -> tuple[list[dict[str, str]], np.ndarray]:
    records = [
        {"source_key": "tie-b", "text": "tie b", "cwd": "/allowed"},
        {"source_key": "tie-a", "text": "tie a", "cwd": "/allowed"},
        {"source_key": "threshold", "text": "threshold", "cwd": "/allowed"},
        {"source_key": "below", "text": "below", "cwd": "/allowed"},
        {"source_key": "excluded", "text": "excluded", "cwd": "/other"},
        {"source_key": "duplicate", "text": "duplicate low", "cwd": "/allowed"},
        {"source_key": "duplicate", "text": "duplicate high", "cwd": "/allowed"},
    ]
    scores = np.asarray([0.8, 0.8, 0.5, 0.49, 0.99, 0.7, 0.95], dtype=np.float32)
    vectors = np.column_stack((scores, np.sqrt(1.0 - scores**2))).astype(np.float32)
    context._write_vector_index_atomic(
        path,
        vectors,
        [record["source_key"] for record in records],
        [f'["{record["cwd"]}"]' for record in records],
        [f"sha-{index}" for index in range(len(records))],
    )
    return records, np.asarray([1.0, 0.0], dtype=np.float32)


def _write_large_vector_index(
    path: Path,
    *,
    row_count: int = 20_000,
    dimension: int = 384,
) -> tuple[list[dict[str, str]], np.ndarray]:
    scores = np.arange(row_count, dtype=np.float32) / row_count
    vectors = np.zeros((row_count, dimension), dtype=np.float32)
    vectors[:, 0] = scores
    vectors[:, 1] = np.sqrt(1.0 - scores**2)
    records = [
        {
            "source_key": f"unit-{index:05d}",
            "text": f"requirement {index}",
            "cwd": "/allowed" if index % 2 == 0 else "/other",
        }
        for index in range(row_count)
    ]
    context._write_vector_index_atomic(
        path,
        vectors,
        [record["source_key"] for record in records],
        [json.dumps([record["cwd"]]) for record in records],
        [f"sha-{index}" for index in range(row_count)],
    )
    query = np.zeros(dimension, dtype=np.float32)
    query[0] = 1.0
    return records, query


def test_bounded_vector_query_filters_deduplicates_and_ranks(tmp_path: Path) -> None:
    index_path = tmp_path / "vectors.npz"
    records, query = _write_index(index_path)

    matches, error, total_qualifying = context._run_vector_query(
        db_path=index_path,
        records=records,
        id_field="source_key",
        query_vec=query,
        allowed_cwds={"/allowed"},
        top_k=3,
        min_score=0.5,
    )

    assert error is None
    assert total_qualifying == 4
    assert [match["source_key"] for match in matches] == [
        "duplicate",
        "tie-a",
        "tie-b",
    ]
    assert matches[0]["text"] == "duplicate high"
    assert matches[0]["vector_score"] == pytest.approx(0.95)


def test_unbounded_vector_query_keeps_thread_search_semantics(tmp_path: Path) -> None:
    index_path = tmp_path / "vectors.npz"
    records, query = _write_index(index_path)

    matches, error, total_qualifying = context._run_vector_query(
        db_path=index_path,
        records=records,
        id_field="source_key",
        query_vec=query,
        allowed_cwds=set(),
    )

    assert error is None
    assert total_qualifying is None
    assert len(matches) == len(records) - 1
    assert [match["vector_score"] for match in matches] == sorted(
        [match["vector_score"] for match in matches],
        reverse=True,
    )


def test_large_bounded_vector_query_execution_latency(tmp_path: Path) -> None:
    row_count = 20_000
    dimension = 384
    index_path = tmp_path / "large-vectors.npz"
    records, query = _write_large_vector_index(
        index_path,
        row_count=row_count,
        dimension=dimension,
    )
    query_kwargs = {
        "db_path": index_path,
        "records": records,
        "id_field": "source_key",
        "query_vec": query,
        "allowed_cwds": {"/allowed"},
        "top_k": 25,
        "min_score": 0.5,
    }
    warm_matches, warm_error, warm_total = context._run_vector_query(**query_kwargs)
    assert warm_error is None
    assert warm_total == 5_000
    assert len(warm_matches) == 25
    expected_ids = [record["source_key"] for record in warm_matches]
    assert expected_ids == [f"unit-{index:05d}" for index in range(19_998, 19_949, -2)]

    samples_ms: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        matches, error, total_qualifying = context._run_vector_query(**query_kwargs)
        samples_ms.append((time.perf_counter() - started) * 1000)
        assert error is None
        assert total_qualifying == 5_000
        assert [record["source_key"] for record in matches] == expected_ids
        assert [record["vector_score"] for record in matches] == sorted(
            [record["vector_score"] for record in matches],
            reverse=True,
        )

    median_ms = statistics.median(samples_ms)
    print(
        "VECTOR_BENCHMARK "
        + json.dumps(
            {
                "corpus_rows": row_count,
                "dimension": dimension,
                "top_k": 25,
                "total_qualifying": warm_total,
                "query_ms_samples": [round(sample, 3) for sample in samples_ms],
                "median_query_ms": round(median_ms, 3),
            },
            sort_keys=True,
        )
    )
    assert median_ms < 1_000.0


def test_unit_vector_search_reports_applied_bounds_and_visible_scores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "vectors.npz"
    records, query = _write_index(index_path)

    def embed(texts: list[str]) -> np.ndarray:
        return np.asarray([query for _text in texts], dtype=np.float32)

    monkeypatch.setattr(context, "_ensure_requirements_importable", lambda: tmp_path)
    monkeypatch.setattr(context, "_load_unit_records", lambda: records)
    monkeypatch.setattr(
        context,
        "_ensure_unit_vector_index",
        lambda _records, embedder: {"ready": True},
    )
    monkeypatch.setattr(context, "_unit_vector_path", lambda: index_path)

    result = context.search_requirement_units_vector(
        query="bounded semantic query",
        all_projects=True,
        top_k=2,
        min_score=0.5,
        embedder=embed,
    )

    assert result["success"] is True
    assert result["count"] == 2
    assert result["top_k"] == 2
    assert result["min_score"] == 0.5
    assert result["total_qualifying"] == 5
    assert result["truncated"] is True
    assert all("source_key" in match for match in result["matches"])
    assert all("vector_score" in match for match in result["matches"])
    assert "max_matches" not in result


def test_capability_payload_requires_strict_vector_bounds() -> None:
    payload = _RequirementsUnitVectorPayload(
        query="semantic",
        top_k=25,
        min_score=0,
    )
    assert payload.top_k == 25
    assert payload.min_score == 0.0

    with pytest.raises(ValueError):
        _RequirementsUnitVectorPayload(query="semantic", top_k=True, min_score=0.0)
    with pytest.raises(ValueError):
        _RequirementsUnitVectorPayload(query="semantic", top_k=25, min_score="0.0")


def test_internal_endpoint_forwards_validated_vector_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def run_search(metric: str, action: str, **kwargs: object) -> dict[str, object]:
        captured.update({"metric": metric, "action": action, **kwargs})
        return {"success": True}

    monkeypatch.setattr(
        requirements_api.extension_store,
        "extension_id_for_role",
        lambda _role: "requirements",
    )
    monkeypatch.setattr(
        requirements_api,
        "require_builtin_runtime_extension",
        lambda _extension_id: None,
    )
    monkeypatch.setattr(requirements_api, "require_internal", lambda: None)
    monkeypatch.setattr(
        requirements_api,
        "run_supervised_requirements_search",
        run_search,
    )

    result = asyncio.run(
        requirements_api.internal_requirements_unit_vector(
            {
                "query": "semantic",
                "cwd": "/repo",
                "top_k": 17,
                "min_score": 0.4,
            },
            x_internal_token="internal",
        )
    )

    assert result == {"success": True}
    assert captured["metric"] == "requirements.unit_vector"
    assert captured["action"] == "unit_vector"
    assert captured["top_k"] == 17
    assert captured["min_score"] == 0.4


@pytest.mark.parametrize("top_k", [0, 201, True, 1.0, "1", None])
def test_top_k_validation_rejects_invalid_values(top_k: object) -> None:
    with pytest.raises(ValueError, match="top_k"):
        validate_unit_vector_bounds(top_k, 0.0)


@pytest.mark.parametrize(
    "min_score",
    [-0.01, 1.01, True, "0.5", None, math.nan, math.inf, -math.inf, 10**1000],
)
def test_min_score_validation_rejects_invalid_values(min_score: object) -> None:
    with pytest.raises(ValueError, match="min_score"):
        validate_unit_vector_bounds(25, min_score)


@pytest.mark.parametrize("min_score", [0, 0.0, 0.5, 1, 1.0])
def test_vector_bounds_accept_finite_numeric_score_range(min_score: object) -> None:
    assert validate_unit_vector_bounds(25, min_score) == (25, float(min_score))
