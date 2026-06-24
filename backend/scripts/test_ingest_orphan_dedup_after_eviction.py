"""Regression test: ingest_orphan must not duplicate events after root eviction.

Scenario (the real bug from session dbfb3852):
  1. Live events flow through apply_event → events.jsonl (seq 1..N)
  2. Root is evicted from session_manager cache → event_ingester.close()
     clears _seen_uuids / _seen_uids_only
  3. OwnedClaudeJsonlTailer is re-acquired with start_offset=0 (stale
     cursor) and re-reads the same CLI lines
  4. ingest_orphan fires for each re-read line — must NOT produce
     duplicate rows in events.jsonl

Before the fix, step 4 produced duplicates because the ingester's
ensure_open seed scan sometimes missed events written just before close
(narrow fsync-to-read race under heavy load). After the fix,
ingest_orphan's pre-flight UUID check skips events already tracked in
_seen_uids_only, and the ingester's own uid:sha256 dedup is the
authoritative second guard.

Run with:
    cd backend && .venv/bin/python scripts/test_ingest_orphan_dedup_after_eviction.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-orphan-dedup-")

from event_ingester import event_ingester
from jsonl_tailer import OwnedClaudeJsonlTailer
from orchs import ApplyEventCtx, get_strategy
from session_manager import manager as session_manager

PASS = "[32mPASS[0m"
FAIL = "[31mFAIL[0m"


def _make_session(sid: str) -> None:
    session_manager._roots[sid] = {
        "id": sid,
        "messages": [],
        "agent_session_id": sid,
        "orchestration_mode": "native",
        "cwd": "/tmp",
        "processed_line_by_sid": {},
    }


def _read_events(root_id: str) -> list[dict]:
    path = Path(_TMP_HOME) / "sessions" / root_id / "events.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _write_cli_lines(jsonl: Path, lines: list[dict]) -> None:
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_cli_event(u: str, text: str = "hello") -> dict:
    return {
        "type": "assistant",
        "uuid": u,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


async def test_ingest_orphan_skips_known_uuids() -> bool:
    """Pre-flight UUID check prevents duplicate events.jsonl rows."""
    sid = str(uuid.uuid4())
    _make_session(sid)
    root_id = session_manager._root_id_for(sid) or sid
    agent_sid = str(uuid.uuid4())
    jsonl = Path(_TMP_HOME) / "sessions" / sid / "agent.jsonl"

    uuids = [str(uuid.uuid4()) for _ in range(5)]
    lines = [_make_cli_event(u) for u in uuids]
    _write_cli_lines(jsonl, lines)

    # Step 1: tailer reads all lines → ingest_orphan writes them.
    owned = OwnedClaudeJsonlTailer(
        root_id=root_id, app_session_id=sid,
        agent_sid=agent_sid, jsonl_path=jsonl, start_offset=0,
    )
    owned.acquire()
    for _ in range(100):
        await asyncio.sleep(0.05)
        evs = _read_events(root_id)
        if len(evs) >= len(lines):
            break
    released = owned.release()
    if released:
        try:
            await released
        except Exception:
            pass

    events_after_first = _read_events(root_id)
    count_first = len(events_after_first)
    if count_first < len(lines):
        print(f"  first pass only ingested {count_first}/{len(lines)}")
        return False

    # Step 2: simulate root eviction — close the ingester, clearing
    # in-memory dedup state.
    event_ingester.close(root_id)

    # Step 3: re-acquire tailer with start_offset=0 (stale cursor).
    owned2 = OwnedClaudeJsonlTailer(
        root_id=root_id, app_session_id=sid,
        agent_sid=agent_sid, jsonl_path=jsonl, start_offset=0,
    )
    owned2.acquire()

    # Wait for the tailer to process all lines.
    for _ in range(100):
        await asyncio.sleep(0.05)
        evs = _read_events(root_id)
        if len(evs) >= count_first:
            # Give it one more cycle for the dedup to settle
            await asyncio.sleep(0.1)
            break
    released2 = owned2.release()
    if released2:
        try:
            await released2
        except Exception:
            pass

    # Step 4: verify no duplicates.
    events_after_reread = _read_events(root_id)
    ingested_uuids = [
        (e.get("data") or {}).get("uuid")
        for e in events_after_reread
    ]
    for u in uuids:
        c = ingested_uuids.count(u)
        if c != 1:
            print(f"  uuid {u[:8]} appears {c} times (expected 1)")
            return False

    # Total count must not have grown.
    if len(events_after_reread) > count_first:
        print(f"  events.jsonl grew from {count_first} to "
              f"{len(events_after_reread)} rows (should be unchanged)")
        return False

    event_ingester.close(root_id)
    return True


async def test_ingest_orphan_after_close_reopen() -> bool:
    """ingest_orphan after ingester close+reopen dedupes by seed scan."""
    sid = str(uuid.uuid4())
    _make_session(sid)
    root_id = session_manager._root_id_for(sid) or sid
    agent_sid = str(uuid.uuid4())
    jsonl = Path(_TMP_HOME) / "sessions" / sid / "agent2.jsonl"

    # Write CLI lines
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
    _write_cli_lines(jsonl, [_make_cli_event(u1, "first")])

    # Ingest first event via the live apply_event path (not tailer).
    strategy = get_strategy("native")
    msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "isStreaming": False,
        "events": [],
    }
    session_manager._roots[sid]["messages"].append(msg)
    ctx = ApplyEventCtx(root_id=root_id)
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event={"type": "agent_message", "data": _make_cli_event(u1, "first")},
        ctx=ctx, source_is_provider_stream=True,
    )

    # apply_event writes via fire-and-forget (timeout=0), so wait for it.
    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(_read_events(root_id)) >= 1:
            break
    events_after_live = _read_events(root_id)
    if len(events_after_live) != 1:
        print(f"  expected 1 event after live ingest, got {len(events_after_live)}")
        return False

    # Close + reopen ingester (simulating eviction).
    event_ingester.close(root_id)
    # Give executor time to fully drain.
    await asyncio.sleep(0.2)

    # Now write a second CLI line and start tailer with offset=0.
    _write_cli_lines(jsonl, [_make_cli_event(u2, "second")])

    owned = OwnedClaudeJsonlTailer(
        root_id=root_id, app_session_id=sid,
        agent_sid=agent_sid, jsonl_path=jsonl, start_offset=0,
    )
    owned.acquire()
    for _ in range(100):
        await asyncio.sleep(0.05)
        evs = _read_events(root_id)
        found_uuids = {(e.get("data") or {}).get("uuid") for e in evs}
        if u2 in found_uuids:
            break
    released = owned.release()
    if released:
        try:
            await released
        except Exception:
            pass

    # u1 must appear exactly once, u2 exactly once.
    events_final = _read_events(root_id)
    final_uuids = [(e.get("data") or {}).get("uuid") for e in events_final]
    c1 = final_uuids.count(u1)
    c2 = final_uuids.count(u2)
    if c1 != 1:
        print(f"  u1 appears {c1} times (expected 1)")
        return False
    if c2 != 1:
        print(f"  u2 appears {c2} times (expected 1)")
        return False

    event_ingester.close(root_id)
    return True


TESTS = [
    ("ingest_orphan skips known UUIDs after eviction", test_ingest_orphan_skips_known_uuids),
    ("ingest_orphan dedupes after close+reopen with seed scan", test_ingest_orphan_after_close_reopen),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = asyncio.run(fn())
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        try:
            event_ingester.close_all()
        except Exception:
            pass
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
