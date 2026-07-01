from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba_trace_meta_")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trace_collector  # noqa: E402
import trace_metadata_index  # noqa: E402
from paths import ba_home  # noqa: E402


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def _reset() -> None:
    home = ba_home()
    if home.exists():
        import shutil

        shutil.rmtree(home)


def _index_path() -> Path:
    path = trace_collector._traces_dir() / "index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_index(lines: list[str]) -> Path:
    path = _index_path()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _save_trace(trace_id: str, prompt: str = "prompt") -> None:
    trace = trace_collector.TraceCollector("s-meta", prompt)
    trace.trace_id = trace_id
    step = trace.start_step("routing")
    step.input_prompt = "x"
    step.raw_output = "y"
    step.end()
    trace.steps.append(step)
    trace.finalize()
    trace.save()


def test_search_semantics(failures: list[str]) -> None:
    _reset()
    dup = json.dumps({"trace_id": "tr_dup", "session_id": "s", "user_prompt_preview": "Needle"})
    _write_index([
        json.dumps({"trace_id": "tr_old", "session_id": "s", "user_prompt_preview": "old"}),
        "{bad",
        "",
        dup,
        json.dumps({"trace_id": "tr_new", "session_id": "s", "user_prompt_preview": "new needle"}),
        dup,
        json.dumps({"trace_id": "tr_nul", "session_id": "s", "user_prompt_preview": "zero\x00needle"}),
    ])
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("needle", limit=10)]
        == ["tr_nul", "tr_dup", "tr_new", "tr_dup"],
        "metadata search preserves newest order and duplicate lines",
        failures,
    )
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("TRACE_ID", limit=2)]
        == ["tr_nul", "tr_dup"],
        "metadata search matches raw JSON keys case-insensitively",
        failures,
    )
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("tr", limit=1)]
        == ["tr_nul"],
        "short metadata search uses literal substring",
        failures,
    )
    _check(
        trace_collector.search_traces("\x00needle", limit=10) == [],
        "metadata NUL search does not overmatch",
        failures,
    )
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("\\u0000needle", limit=10)]
        == ["tr_nul"],
        "metadata search preserves escaped JSON raw-line semantics",
        failures,
    )
    _check(trace_collector.search_traces("needle", limit=0) == [], "metadata limit <= 0 returns empty", failures)


def test_hot_search_and_rebuilds(failures: list[str]) -> None:
    _reset()
    _save_trace("tr_one", "first searchable")
    _save_trace("tr_two", "second searchable")
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("searchable", limit=5)]
        == ["tr_two", "tr_one"],
        "initial metadata search builds index",
        failures,
    )
    original_iter = trace_metadata_index._iter_index_rows

    def fail_iter(path: Path):
        raise AssertionError(f"unexpected index read: {path}")

    trace_metadata_index._iter_index_rows = fail_iter
    try:
        _check(
            [row["trace_id"] for row in trace_collector.search_traces("searchable", limit=1)]
            == ["tr_two"],
            "hot metadata search does not read source index",
            failures,
        )
        _save_trace("tr_three", "third searchable")
        _check(
            [row["trace_id"] for row in trace_collector.search_traces("searchable", limit=3)]
            == ["tr_three", "tr_two", "tr_one"],
            "hot save appends metadata without rebuild",
            failures,
        )
    finally:
        trace_metadata_index._iter_index_rows = original_iter

    db_path = ba_home() / "trace_metadata_index.sqlite3"
    db_path.unlink()
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("third", limit=1)]
        == ["tr_three"],
        "missing metadata DB rebuilds lazily",
        failures,
    )


def test_stale_save_and_failure_isolation(failures: list[str]) -> None:
    _reset()
    _save_trace("tr_base", "base searchable")
    trace_collector.search_traces("base", limit=1)
    db_path = ba_home() / "trace_metadata_index.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE trace_metadata_meta SET value = '0' WHERE key = 'index_size'")
    conn.commit()
    conn.close()
    original_iter = trace_metadata_index._iter_index_rows

    def fail_iter(path: Path):
        raise AssertionError("save must not rebuild stale metadata index")

    trace_metadata_index._iter_index_rows = fail_iter
    try:
        _save_trace("tr_stale", "stale searchable")
    finally:
        trace_metadata_index._iter_index_rows = original_iter
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("stale", limit=1)]
        == ["tr_stale"],
        "stale metadata DB rebuilds on later search",
        failures,
    )

    original_index = trace_metadata_index.index_appended_entry_under_lock

    def fail_index(*args, **kwargs):
        raise RuntimeError("forced metadata failure")

    trace_metadata_index.index_appended_entry_under_lock = fail_index
    try:
        _save_trace("tr_failure", "failure searchable")
    finally:
        trace_metadata_index.index_appended_entry_under_lock = original_index
    trace_path = trace_collector._traces_dir() / "s-meta" / "tr_failure.json"
    _check(trace_path.exists(), "metadata update failure does not drop full trace JSON", failures)
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("failure", limit=1)]
        == ["tr_failure"],
        "metadata update failure recovers by rebuild",
        failures,
    )


def test_no_final_newline_append_boundary(failures: list[str]) -> None:
    _reset()
    path = _write_index([json.dumps({"trace_id": "tr_old", "session_id": "s", "user_prompt_preview": "old"})])
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    trace_collector.search_traces("old", limit=1)
    _save_trace("tr_newline", "newline searchable")
    lines = path.read_text(encoding="utf-8").splitlines()
    _check(len(lines) == 2, "append repairs missing final newline before writing", failures)
    _check(
        [row["trace_id"] for row in trace_collector.search_traces("searchable", limit=1)]
        == ["tr_newline"],
        "metadata search agrees after newline repair",
        failures,
    )


def test_high_cardinality_query_uses_index(failures: list[str]) -> None:
    _reset()
    path = _index_path()
    with path.open("w", encoding="utf-8") as handle:
        for i in range(20_000):
            handle.write(json.dumps({
                "trace_id": f"tr_{i:05d}",
                "session_id": "s",
                "user_prompt_preview": "bulk",
            }) + "\n")
    trace_collector.search_traces("trace_id", limit=50)
    original_iter = trace_metadata_index._iter_index_rows

    def fail_iter(path: Path):
        raise AssertionError("hot high-cardinality query must not read index source")

    trace_metadata_index._iter_index_rows = fail_iter
    try:
        rows = trace_collector.search_traces("trace_id", limit=50)
    finally:
        trace_metadata_index._iter_index_rows = original_iter
    _check(
        len(rows) == 50 and rows[0]["trace_id"] == "tr_19999",
        "high-cardinality metadata query returns newest limit from index",
        failures,
    )


def main() -> int:
    failures: list[str] = []
    try:
        test_search_semantics(failures)
        test_hot_search_and_rebuilds(failures)
        test_stale_save_and_failure_isolation(failures)
        test_no_final_newline_append_boundary(failures)
        test_high_cardinality_query_uses_index(failures)
    finally:
        import shutil

        shutil.rmtree(os.environ["BETTER_AGENT_HOME"], ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\ntrace metadata index checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
