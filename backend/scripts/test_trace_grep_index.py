from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from unittest import mock
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba_trace_grep_")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trace_collector  # noqa: E402
import trace_grep_index  # noqa: E402
from paths import ba_home  # noqa: E402


def _make_trace(
    session_id: str,
    trace_id: str,
    prompt: str,
    output: str,
    *,
    step_type: str = "routing",
    thread_name: str = "main",
) -> trace_collector.TraceCollector:
    trace = trace_collector.TraceCollector(session_id, f"user {trace_id}")
    trace.trace_id = trace_id
    step = trace.start_step(step_type, thread_name=thread_name)
    step.input_prompt = prompt
    step.raw_output = output
    step.end()
    trace.steps.append(step)
    trace.finalize()
    trace.save()
    return trace


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def _reset() -> None:
    home = ba_home()
    if home.exists():
        import shutil

        shutil.rmtree(home)


def test_shape_order_filters_and_context(failures: list[str]) -> None:
    _reset()
    _make_trace("s-a", "tr_aaa", "old Alpha prompt", "old Alpha output")
    _make_trace("s-z", "tr_zzz", "new Alpha prompt\nnext", "new Alpha output")
    matches = trace_collector.grep_traces("alpha", limit=10)
    _check(
        [(m["trace_id"], m["matched_field"]) for m in matches]
        == [
            ("tr_zzz", "input_prompt"),
            ("tr_zzz", "raw_output"),
            ("tr_aaa", "input_prompt"),
            ("tr_aaa", "raw_output"),
        ],
        "legacy order and duplicate prompt/output rows",
        failures,
    )
    _check(matches[0]["match_context"] == "new Alpha prompt\nnext", "context preserves newline", failures)
    _check(
        [m["matched_field"] for m in trace_collector.grep_traces("ALPHA", field="prompts")]
        == ["input_prompt", "input_prompt"],
        "case-insensitive prompt filter",
        failures,
    )
    _check(
        [m["matched_field"] for m in trace_collector.grep_traces("alpha", field="outputs")]
        == ["raw_output", "raw_output"],
        "output filter",
        failures,
    )
    _check(
        [m["session_id"] for m in trace_collector.grep_traces("alpha", session_id="s-a")]
        == ["s-a", "s-a"],
        "session_id filter uses metadata",
        failures,
    )
    _check(
        trace_collector.grep_traces("alpha", step_type="missing") == [],
        "step_type filter",
        failures,
    )
    _check(trace_collector.grep_traces("alpha", field="bad") == [], "unknown field returns empty", failures)
    _check(len(trace_collector.grep_traces("alpha", limit=1)) == 1, "limit truncates ordered rows", failures)


def test_unicode_and_nul(failures: list[str]) -> None:
    _reset()
    _make_trace("s-nul", "tr_nul", "zero\x00needle only", "plain")
    _make_trace("s-other", "tr_other", "zero without", "plain")
    matches = trace_collector.grep_traces("\x00needle", limit=10)
    _check([m["trace_id"] for m in matches] == ["tr_nul"], "NUL pattern does not overmatch", failures)
    _make_trace("s-u", "tr_unicode", "Straße marker", "plain")
    _check(
        [m["trace_id"] for m in trace_collector.grep_traces("straße")]
        == ["tr_unicode"],
        "unicode lower semantics",
        failures,
    )


def test_hot_search_rebuilds_and_idempotency(failures: list[str]) -> None:
    _reset()
    trace = _make_trace("s-hot", "tr_hot", "hot indexed prompt", "hot output")
    trace_collector.grep_traces("indexed")
    original_loader = trace_grep_index._load_trace_file

    def fail_loader(path: Path):
        raise AssertionError(f"unexpected trace JSON load: {path}")

    trace_grep_index._load_trace_file = fail_loader
    try:
        _check(
            [m["trace_id"] for m in trace_collector.grep_traces("indexed")]
            == ["tr_hot"],
            "hot search does not load trace JSON",
            failures,
        )
    finally:
        trace_grep_index._load_trace_file = original_loader

    db_path = ba_home() / "trace_grep_index.sqlite3"
    db_path.unlink()
    _check(
        [m["trace_id"] for m in trace_collector.grep_traces("indexed")]
        == ["tr_hot"],
        "missing DB rebuilds lazily",
        failures,
    )

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM trace_grep_files")
    conn.commit()
    conn.close()
    trace_path = trace_collector._traces_dir() / trace.session_id / f"{trace.trace_id}.json"
    trace_path.write_text(trace_path.read_text(encoding="utf-8").replace("indexed", "repaired"), encoding="utf-8")
    _check(
        [m["trace_id"] for m in trace_collector.grep_traces("repaired")]
        == ["tr_hot"],
        "manifest mismatch repairs stale index",
        failures,
    )

    trace_grep_index.index_trace(trace.to_dict(), trace_path)
    trace_grep_index.index_trace(trace.to_dict(), trace_path)
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM trace_grep_rows WHERE trace_id = ?", ("tr_hot",)).fetchone()[0]
    conn.close()
    _check(count == 2, "index_trace is idempotent per trace field", failures)


def _write_index(rows: list[str], *, final_newline: bool = True) -> Path:
    traces = trace_collector._traces_dir()
    traces.mkdir(parents=True, exist_ok=True)
    path = traces / "index.jsonl"
    path.write_text("\n".join(rows) + ("\n" if final_newline else ""), encoding="utf-8")
    return path


def test_list_traces_uses_bounded_reverse_index_reads(failures: list[str]) -> None:
    _reset()
    rows = [
        '{"trace_id":"tr_1","session_id":"s-a","user_prompt_preview":"old"}',
        '{"trace_id":"tr_2","session_id":"s-b","user_prompt_preview":"mid"}',
        '{"trace_id":"tr_3","session_id":"s-a","user_prompt_preview":"new"}',
        '{"trace_id":"tr_4","session_id":"s-b","user_prompt_preview":"newest"}',
    ]
    _write_index(rows, final_newline=False)
    original_read_text = Path.read_text

    def guarded_read_text(self: Path, *args, **kwargs):
        if self.name == "index.jsonl":
            raise AssertionError("list_traces must not read the whole index")
        return original_read_text(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", guarded_read_text):
        latest = trace_collector.list_traces(limit=3)
    _check(
        [row["trace_id"] for row in latest] == ["tr_4", "tr_3", "tr_2"],
        "list_traces returns newest rows without final newline",
        failures,
    )
    _check(
        [row["trace_id"] for row in trace_collector.list_traces(session_id="s-a", limit=2)]
        == ["tr_3", "tr_1"],
        "list_traces session filter scans backward until enough matches",
        failures,
    )
    _check(trace_collector.list_traces(limit=0) == [], "list_traces limit <= 0 returns empty", failures)


def test_reverse_index_reader_edge_cases(failures: list[str]) -> None:
    _reset()
    _write_index([], final_newline=False)
    _check(trace_collector.list_traces() == [], "empty trace index returns empty", failures)
    _write_index([
        '{"trace_id":"tr_crlf","session_id":"s"}\r',
        '{"trace_id":"tr_utf","session_id":"s","user_prompt_preview":"🙂"}',
        '{"trace_id":',
    ], final_newline=False)
    _check(
        [row["trace_id"] for row in trace_collector.list_traces(limit=2)]
        == ["tr_utf", "tr_crlf"],
        "reverse reader skips corrupt tail and handles CRLF",
        failures,
    )
    path = trace_collector._traces_dir() / "index.jsonl"
    lines = list(trace_collector._iter_file_lines_reverse(path, _chunk_size=5))
    _check(
        any("🙂" in line for line in lines),
        "reverse reader decodes multibyte UTF-8 after line assembly",
        failures,
    )
    forward = [row["trace_id"] for row in trace_collector.iter_trace_index()]
    _check(forward == ["tr_crlf", "tr_utf"], "iter_trace_index remains oldest-first", failures)


def main() -> int:
    failures: list[str] = []
    try:
        test_shape_order_filters_and_context(failures)
        test_unicode_and_nul(failures)
        test_hot_search_rebuilds_and_idempotency(failures)
        test_list_traces_uses_bounded_reverse_index_reads(failures)
        test_reverse_index_reader_edge_cases(failures)
    finally:
        import shutil

        shutil.rmtree(os.environ["BETTER_AGENT_HOME"], ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\ntrace grep index checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
