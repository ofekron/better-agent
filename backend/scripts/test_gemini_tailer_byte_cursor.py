"""Regression: GeminiJsonlTailer must read session_events.jsonl by a
persisted byte cursor, NOT re-read the whole file from line 0 on every
poll.

Pre-fix `_read_new_lines` skipped `processed_offset` lines via a
readline() loop from the top on EVERY 50ms poll — O(total_lines) per
poll, O(n^2) over a turn. For ba_runner (and Gemini) turns the runner
writes a cumulative replacement line per text delta, so the file grows
large fast; re-reading it each poll lagged the UI and stalled the
deterministic drain (`await_line_tailer_drained`), leaving stale render
trees. Native Claude sessions never hit this because they tail by byte
offset.

The fix keeps `processed_offset` (line count) as the persisted recovery
cursor and adds an in-memory `_byte_cursor` that primes once from the
line count, then seeks+reads only new bytes per poll. These tests lock:
  1. incremental reads return ONLY appended lines (no from-top re-scan);
  2. the byte cursor tracks the file size exactly;
  3. a recovery resume (start_offset) primes past the right line count;
  4. the base run() loop still advances the line-count cursor for
     persistence and dispatches every event.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _test_home
_test_home.isolate("bc_gemini_byte_cursor_")

from jsonl_tailer import GeminiJsonlTailer  # noqa: E402


def _line(i: int) -> str:
    # Cumulative-replacement shape the runner writes per delta: the same
    # uuid, text grows by one char each line. Bloats the file like a real
    # streamed turn so the O(n^2) re-read would dominate pre-fix.
    return json.dumps(
        {"type": "assistant", "uuid": "u", "message": {"text": "x" * (i + 1)}}
    )


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_byte_cursor_reads_only_new_lines_without_rescan(tmp_path: Path) -> None:
    p = tmp_path / "session_events.jsonl"
    n = 4000
    _write(p, [_line(i) for i in range(n)])

    tailer = GeminiJsonlTailer(path=p, start_offset=0, dispatch=lambda e: None)

    # First pass reads everything; cursor lands at EOF.
    first = tailer._read_new_lines()
    assert len(first) == n, f"expected {n} lines, got {len(first)}"
    assert tailer._cursor_ready
    assert tailer._byte_cursor == p.stat().st_size

    # No new data -> must NOT re-scan the file (the pre-fix regression).
    # O(n^2) re-read of this multi-MB file would blow the guard below.
    start = time.perf_counter()
    assert tailer._read_new_lines() == []
    reread_s = time.perf_counter() - start
    assert reread_s < 0.25, f"incremental read re-scanned the file: {reread_s:.3f}s"

    # Append exactly one line -> incremental read returns only it and
    # advances the cursor to the new EOF.
    with p.open("a", encoding="utf-8") as f:
        f.write(_line(n) + "\n")
    appended = tailer._read_new_lines()
    assert len(appended) == 1, f"expected 1 appended line, got {len(appended)}"
    assert appended[0] == _line(n)
    assert tailer._byte_cursor == p.stat().st_size


def test_byte_cursor_primes_from_start_offset_for_recovery(tmp_path: Path) -> None:
    p = tmp_path / "session_events.jsonl"
    lines = [_line(i) for i in range(20)]
    _write(p, lines)
    skip = 12  # simulate a recovery re-attach mid-run

    tailer = GeminiJsonlTailer(path=p, start_offset=skip, dispatch=lambda e: None)

    # Prime must read past `skip` lines and return only the remainder.
    got = tailer._read_new_lines()
    assert got == lines[skip:], "recovery resume must skip already-processed lines"
    assert tailer._byte_cursor == p.stat().st_size
    assert tailer.processed_offset == skip  # base cursor untouched until dispatch


def test_run_loop_dispatches_all_and_advances_line_cursor(tmp_path: Path) -> None:
    p = tmp_path / "session_events.jsonl"
    n = 300
    _write(p, [_line(i) for i in range(n)])

    seen: list[dict] = []
    tailer = GeminiJsonlTailer(
        path=p, start_offset=0, dispatch=lambda e: seen.append(e)
    )
    # Drain then stop so run() terminates cleanly.
    asyncio.run(_drain_until_done(tailer, expected=n))

    assert len(seen) == n
    assert tailer.processed_offset == n  # persisted-recovery cursor correct
    assert tailer._byte_cursor == p.stat().st_size


async def _drain_until_done(tailer: GeminiJsonlTailer, expected: int) -> None:
    task = asyncio.create_task(tailer.run())
    # Poll until every line is dispatched, then stop.
    deadline = asyncio.get_running_loop().time() + 5.0
    try:
        while tailer.processed_offset < expected:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"timed out: dispatched {tailer.processed_offset}/{expected}"
                )
            await asyncio.sleep(0.01)
    finally:
        tailer.stop()
        await task


def test_primes_lazily_when_file_appears_late(tmp_path: Path) -> None:
    p = tmp_path / "session_events.jsonl"
    tailer = GeminiJsonlTailer(path=p, start_offset=0, dispatch=lambda e: None)
    assert tailer._read_new_lines() == []  # file absent: prime is a no-op, returns []

    _write(p, [_line(i) for i in range(5)])
    assert len(tailer._read_new_lines()) == 5
    assert tailer._byte_cursor == p.stat().st_size


if __name__ == "__main__":
    import tempfile

    failures: list[str] = []
    for name, fn in [
        ("byte_cursor_reads_only_new_lines_without_rescan",
         test_byte_cursor_reads_only_new_lines_without_rescan),
        ("byte_cursor_primes_from_start_offset_for_recovery",
         test_byte_cursor_primes_from_start_offset_for_recovery),
        ("run_loop_dispatches_all_and_advances_line_cursor",
         test_run_loop_dispatches_all_and_advances_line_cursor),
        ("primes_lazily_when_file_appears_late",
         test_primes_lazily_when_file_appears_late),
    ]:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {exc!r}")
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
