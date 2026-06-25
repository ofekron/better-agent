"""Regression: WS subscriber gap-fill paginates past one reader page.

The bug: `_Subscriber._fill_gap` did a single `read_events(limit=10_000)`
and ignored `has_more`, so a gap larger than 10k rows delivered only the
first page and the watermark then advanced over the undelivered hole.

The fix loops on `has_more` until the gap is closed (bounded by
`until_seq`). This test journals >10,000 rows for one sid and asserts a
subscriber catching up from seq 0 receives every row contiguously.

Run with:
    cd backend && .venv/bin/python scripts/test_ws_gapfill_pagination.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-gapfill-")

from event_ingester import event_ingester  # noqa: E402
from jsonl_tailer import _Subscriber  # noqa: E402

ROW_COUNT = 10_500  # one full 10k reader page + a remainder page


def _seed_rows(root_id: str, sid: str) -> int:
    batch = [
        (
            sid,
            "agent_message",
            {
                "uuid": f"u{i}",
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"row {i}"}]},
            },
            "test",
            None,
            None,
        )
        for i in range(ROW_COUNT)
    ]
    seqs = event_ingester.ingest_batch(root_id, batch)
    assert all(s > 0 for s in seqs), "seed batch had deduped rows"
    return max(seqs)


async def _run() -> None:
    root_id = "gapfill-root"
    sid = root_id
    cursor = _seed_rows(root_id, sid)
    assert cursor == ROW_COUNT, cursor

    frames: list[dict] = []

    async def _collect(frame: dict) -> None:
        frames.append(frame)

    sub = _Subscriber(
        app_session_id=sid,
        ws_callback=_collect,
        from_seq=0,
        root_id=root_id,
    )
    await sub.catch_up_to(cursor)

    seqs = [f.get("seq") for f in frames]
    assert len(frames) == ROW_COUNT, (
        f"expected {ROW_COUNT} frames, got {len(frames)} "
        f"(last seq {seqs[-1] if seqs else None})"
    )
    assert seqs == list(range(1, ROW_COUNT + 1)), (
        f"non-contiguous delivery: first={seqs[:3]} last={seqs[-3:]}"
    )
    assert sub.next_seq == cursor + 1, sub.next_seq

    first_connection: list[int] = []

    async def _reject_at_100(frame: dict) -> bool:
        seq = frame.get("seq")
        if seq == 100:
            return False
        first_connection.append(seq)
        return True

    interrupted = _Subscriber(
        app_session_id=sid,
        ws_callback=_reject_at_100,
        from_seq=0,
        root_id=root_id,
    )
    await interrupted.catch_up_to(cursor)
    assert first_connection == list(range(1, 100)), first_connection[-3:]
    assert interrupted.next_seq == 100, interrupted.next_seq

    resumed_frames: list[dict] = []

    async def _resume(frame: dict) -> bool:
        resumed_frames.append(frame)
        return True

    resumed = _Subscriber(
        app_session_id=sid,
        ws_callback=_resume,
        from_seq=interrupted.next_seq - 1,
        root_id=root_id,
    )
    await resumed.catch_up_to(cursor)
    resumed_seqs = [frame.get("seq") for frame in resumed_frames]
    assert resumed_seqs == list(range(100, cursor + 1)), resumed_seqs[:3]
    assert resumed.next_seq == cursor + 1, resumed.next_seq

    boundary_frames: list[dict] = []

    async def _boundary_collect(frame: dict) -> bool:
        boundary_frames.append(frame)
        return True

    boundary = _Subscriber(
        app_session_id=sid,
        ws_callback=_boundary_collect,
        from_seq=cursor - 3,
        root_id=root_id,
    )
    await boundary.push_entry(
        {"seq": cursor},
        {"type": "agent_message", "data": {}, "seq": cursor},
    )
    boundary_seqs = [frame.get("seq") for frame in boundary_frames]
    assert boundary_seqs == [cursor - 2, cursor - 1, cursor], boundary_seqs
    assert boundary.next_seq == cursor + 1, boundary.next_seq


def main() -> int:
    try:
        asyncio.run(_run())
        print("PASS test_ws_gapfill_pagination")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
