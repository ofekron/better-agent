"""Regression: `ingest_orphan`'s pre-flight dedup uses the journal's
canonical uid:sha256(data) rule, not uid-only.

The bug: the pre-flight shortcut checked uid membership alone, so a
same-uid event with MUTATED data (a streaming update the journal's own
rule would append as a new row) was silently dropped before reaching the
ingester.

Asserts:
  1. live-ingest {uuid:X, data:A}, then ingest_orphan {uuid:X, data:B}
     → a second journal row IS appended (fails pre-fix).
  2. ingest_orphan {uuid:X, data:A} again → NO new row.

Run with:
    cd backend && .venv/bin/python scripts/test_ingest_orphan_dedup.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-orphan-dedup-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402

ROOT = "orphan-dedup-root"


def _agent_data(uuid: str, text: str) -> dict:
    return {
        "uuid": uuid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _orphan(data: dict) -> None:
    get_strategy("native").ingest_orphan(
        app_session_id=ROOT,
        event={"type": "agent_message", "data": data},
        ctx=ApplyEventCtx(root_id=ROOT),
        source_is_provider_stream=True,
    )


def _rows() -> list[dict]:
    events, _, _ = event_ingester.read_events(ROOT, limit=999_999)
    return events


def _wait_rows(expected: int, label: str) -> list[dict]:
    """The orphan write is fire-and-forget onto the journal writer's
    per-root executor; poll until the expected row count lands."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        rows = _rows()
        if len(rows) >= expected:
            return rows
        time.sleep(0.02)
    raise AssertionError(
        f"{label}: expected {expected} rows, got {len(_rows())}"
    )


def main() -> int:
    try:
        data_a = _agent_data("X", "text A")
        data_b = _agent_data("X", "text B")

        # Live ingest of {X, A} — synchronous, seeds _seen_uuids.
        seq = event_ingester.ingest(
            ROOT, ROOT, "agent_message", dict(data_a), source="apply_event",
        )
        assert seq == 1, seq

        # Same uid, MUTATED data → the journal rule appends a new row;
        # the pre-flight shortcut must not swallow it.
        _orphan(dict(data_b))
        rows = _wait_rows(2, "orphan {X,B}")
        texts = [
            r["data"]["message"]["content"][0]["text"] for r in rows
        ]
        assert texts == ["text A", "text B"], texts

        # Same uid, SAME data → dedup, no new row. A distinct sentinel
        # orphan after it proves the executor drained (per-root writes
        # are serialized), so the absence of a third X row is real.
        _orphan(dict(data_a))
        _orphan(_agent_data("Y", "sentinel"))
        rows = _wait_rows(3, "sentinel {Y}")
        assert len(rows) == 3, [r["data"].get("uuid") for r in rows]
        uuids = [r["data"].get("uuid") for r in rows]
        assert uuids == ["X", "X", "Y"], uuids

        print("PASS test_ingest_orphan_dedup")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
