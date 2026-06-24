"""Locks the v8 migration `dedupe_by_uid_only=True` contract.

Pre-fix: `_v7_to_v8_migrate` walks `msg.events` (stored as normalized
inner `agent_message`) and ingests as `event_type=agent_message`. Live
`apply_event` had previously written the OUTER `manager_event` wrapper
to events.jsonl. Different shape → different sha256 → uid:sha256 dedup
misses → migration adds DUPLICATE rows.

On session `4ddbd4d7` measured **4346 dup rows** (38% of 11488 total).

Fix: `_v7_to_v8_migrate` passes `dedupe_by_uid_only=True` to
`event_ingester.ingest`. The ingester maintains a per-root `_seen_uids_only`
set populated at boot scan + every write. Migration ingest now skips
when the uid is on ANY row regardless of shape.

This test:
  1. Pre-populate events.jsonl with `manager_event`-shaped rows
     (mimicking live ingest).
  2. Forge a v7 record whose `msg.events` holds the unwrapped inner
     `agent_message` shape (same uid, different data).
  3. Run the v7→v8 migration via `session_manager.get_root_tree_paginated`.
  4. Assert events.jsonl row count is UNCHANGED (no dups added).
  5. Hydration reconstructs msg.events correctly.

Run with:
    cd backend && .venv/bin/python scripts/test_v8_migration_uid_only_dedup.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-v8-uid-dedup-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from paths import ba_home  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _events_jsonl_path(root_id: str):
    return ba_home() / "sessions" / root_id / "events.jsonl"


def _events_jsonl_count(root_id: str) -> int:
    p = _events_jsonl_path(root_id)
    if not p.exists():
        return 0
    with open(p) as f:
        return sum(1 for line in f if line.strip())


def _inner_agent_message(uid: str, text: str) -> dict:
    """Normalized inner shape — what `msg.events` stores after
    `_normalize_for_render` unwrapped the manager_event."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _outer_manager_event_data(uid: str, text: str) -> dict:
    """Outer manager_event wrapper data — what events.jsonl holds
    after live ingest."""
    return {
        "event": {
            "type": "agent_message",
            "data": {
                "uuid": uid,
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            },
        },
    }


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    sid = "v8-dedup-root"
    N = 30
    uids = [f"u-{i}" for i in range(N)]

    # 1) Pre-populate events.jsonl with outer manager_event shape via
    # event_ingester.ingest (mimicking how live apply_event writes).
    for u in uids:
        event_ingester.ingest(
            sid, sid, "manager_event",
            _outer_manager_event_data(u, f"text-{u}"),
            source="apply_event", msg_id="msg-1",
            cwd_override="/tmp/v8-dedup",
        )
    pre_count = _events_jsonl_count(sid)
    results.append(
        ("pre-migration events.jsonl has N manager_event rows",
         pre_count == N, f"got {pre_count}, expected {N}"))

    # 2) Forge v7 record with embedded msg.events in INNER agent_message
    # shape (the shape that `_normalize_for_render` produces from a
    # manager_event wrapper).
    record = {
        "_schema_version": 7,
        "id": sid,
        "name": "v8-dedup-test",
        "model": "sonnet",
        "cwd": "/tmp/v8-dedup",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [
            {
                "id": "msg-1",
                "role": "assistant",
                "content": "",
                "events": [_inner_agent_message(u, f"text-{u}") for u in uids],
                "seq": 1,
            },
        ],
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "source": "cli",
    }
    snapshot_path = ba_home() / "sessions" / f"{sid}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with open(snapshot_path, "w") as f:
        json.dump(record, f)

    # 3) Load via session_manager → triggers v7→v8 migration. With
    # `dedupe_by_uid_only=True` the migration should SKIP ingesting
    # any of the N events because their uids are already on disk.
    tree = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    assert tree is not None
    post_count = _events_jsonl_count(sid)
    results.append(
        ("post-migration events.jsonl unchanged — NO duplicate rows",
         post_count == pre_count,
         f"pre={pre_count} post={post_count} (delta={post_count - pre_count})"))

    # 4) Hydration still produced N events on msg.events (from the
    # pre-existing manager_event rows, unwrapped by apply_event).
    hydrated = tree["messages"][0].get("events") or []
    uids_in_msg = {
        e["data"]["uuid"] for e in hydrated
        if isinstance(e.get("data"), dict)
    }
    results.append(
        ("hydrated msg.events has every uid (rebuilt from outer rows)",
         uids_in_msg == set(uids),
         f"missing={set(uids) - uids_in_msg}"))

    # 5) Negative case: when events.jsonl is EMPTY, migration DOES
    # ingest (uid-only dedup is a no-op when nothing is seen).
    sid2 = "v8-dedup-empty-jsonl"
    event_ingester._handles.pop(sid2, None)
    event_ingester._seq.pop(sid2, None)
    event_ingester._seen_uuids.pop(sid2, None)
    event_ingester._seen_uids_only.pop(sid2, None)
    record2 = dict(record, id=sid2)
    record2["messages"] = [
        {**record["messages"][0], "id": "msg-2"},
    ]
    p2 = ba_home() / "sessions" / f"{sid2}.json"
    with open(p2, "w") as f:
        json.dump(record2, f)
    _ = session_manager.get_root_tree_paginated(sid2, msg_limit=50)
    cnt2 = _events_jsonl_count(sid2)
    results.append(
        ("empty events.jsonl: migration backfills every event",
         cnt2 == N, f"got {cnt2}, expected {N}"))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
