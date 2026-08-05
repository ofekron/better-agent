from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import requirement_context as context


ROW_COUNT = 20_000
EXPECTED_FTS_HITS = 2_000
EXPECTED_RETURNED_ROWS = 1_000


def test_large_unit_fts_query_execution_latency(tmp_path: Path) -> None:
    source_path = tmp_path / "requirement_units.jsonl"
    source_path.write_text("benchmark corpus\n", encoding="utf-8")
    db_path = tmp_path / "requirement_units_fts.sqlite3"
    records = [
        {
            "source_key": f"unit-{index:05d}",
            "text": (
                f"benchmarkneedle requirement {index}"
                if index % 10 == 0
                else f"unrelated requirement {index}"
            ),
            "cwd": "/allowed" if index % 20 == 0 else "/other",
            "ts": f"2026-01-01T00:{index % 60:02d}:00Z",
        }
        for index in range(ROW_COUNT)
    ]
    index_result = context._ensure_fts_index(
        db_path=db_path,
        table_name=context.UNIT_FTS_TABLE,
        source_path=source_path,
        records=records,
        search_text_fn=lambda record: record["text"],
        cwds_fn=lambda record: [record["cwd"]],
        sort_key_fn=lambda record: record["source_key"],
    )
    assert index_result["ready"] is True
    assert index_result["record_count"] == ROW_COUNT

    query_kwargs = {
        "db_path": db_path,
        "table_name": context.UNIT_FTS_TABLE,
        "match_expr": '"benchmarkneedle"',
        "allowed_cwds": {"/allowed"},
        "id_field": "source_key",
    }
    all_matches, all_error = context._run_fts_query(
        **{**query_kwargs, "allowed_cwds": set()}
    )
    assert all_error is None
    assert len(all_matches) == EXPECTED_FTS_HITS
    warm_matches, warm_error = context._run_fts_query(**query_kwargs)
    assert warm_error is None
    assert len(warm_matches) == EXPECTED_RETURNED_ROWS

    samples_ms: list[float] = []
    expected_ids = [record["source_key"] for record in warm_matches]
    assert len(set(expected_ids)) == EXPECTED_RETURNED_ROWS
    for _ in range(5):
        started = time.perf_counter()
        matches, error = context._run_fts_query(**query_kwargs)
        samples_ms.append((time.perf_counter() - started) * 1000)
        assert error is None
        assert [record["source_key"] for record in matches] == expected_ids

    median_ms = statistics.median(samples_ms)
    print(
        "FTS_BENCHMARK "
        + json.dumps(
            {
                "corpus_rows": ROW_COUNT,
                "fts_hits": EXPECTED_FTS_HITS,
                "returned_rows": EXPECTED_RETURNED_ROWS,
                "query_ms_samples": [round(sample, 3) for sample in samples_ms],
                "median_query_ms": round(median_ms, 3),
            },
            sort_keys=True,
        )
    )
    assert median_ms < 750.0
