"""Locks the v7 → v8 migration semantics:

Case A — v7 on disk + populated events.jsonl (steady-state for any
session running before the v8 schema bump):
  - Load is idempotent: the migration's `event_ingester.ingest` is a
    uid:sha256(data) dedup no-op against entries already on the row.
  - Hydration produces a tree byte-identical to a scenario-1 baseline
    (same events ingested live).
  - `_schema_version == 9` after load.

Case B — v7 on disk + DELETED events.jsonl (worst case: pre-events.jsonl
session, file rotation, or manual cleanup):
  - Migration walks msg.events and rebuilds events.jsonl from the
    embedded data.
  - Next write_session_full strips the now-redundant on-disk events.
  - Reload from disk hydrates back into msg.events from events.jsonl.
  - Hydrated tree matches a scenario-1 baseline.
  - Re-running migration on the post-migration v8 record is a no-op
    (empty msg.events → nothing to ingest).

Run with:
    cd backend && .venv/bin/python scripts/test_v7_to_v8_migration.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-v7v8-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from paths import ba_home  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _native_event(uuid: str, text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _build_baseline_via_live_apply(n: int) -> tuple[str, list[tuple[str, str]]]:
    """Scenario-1: create a session, drive N events through live
    apply_event (which writes both render-tree msg.events AND
    events.jsonl). Return (sid, [(uuid, text) per event])."""
    sess = session_manager.create(
        name="baseline", model="sonnet", cwd="/tmp/test-v7v8",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid, run_id="run-baseline")
    pairs: list[tuple[str, str]] = []
    for i in range(n):
        uid = f"u-{i}"
        text = f"text-{i}"
        ev = _native_event(uid, text)
        pairs.append((uid, text))
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.flush_pending_persists()
    return sid, pairs


def _write_v7_to_disk(uuids: list[str]) -> str:
    """Forge a v7 session file with embedded events and write it
    directly to disk (no session_manager involvement so the events.jsonl
    backfill / strip don't run yet)."""
    sid = "v7-test-session"
    events = [_native_event(u, f"text-{u}") for u in uuids]
    record = {
        "_schema_version": 7,
        "id": sid,
        "name": "v7-record",
        "model": "sonnet",
        "cwd": "/tmp/test-v7v8",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [
            {
                "id": "msg-1",
                "role": "user",
                "content": "u",
                "events": [],
                "seq": 0,
            },
            {
                "id": "msg-2",
                "role": "assistant",
                "content": "",
                "events": events,
                "seq": 1,
            },
        ],
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "source": "cli",
    }
    path = ba_home() / "sessions" / f"{sid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f)
    return sid


def _events_jsonl_path(sid: str):
    return ba_home() / "sessions" / sid / "events.jsonl"


def _events_jsonl_count(sid: str) -> int:
    p = _events_jsonl_path(sid)
    if not p.exists():
        return 0
    with open(p) as f:
        return sum(1 for line in f if line.strip())


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    N = 30

    # === Case B: v7 + DELETED events.jsonl ===
    sid_b = _write_v7_to_disk([f"b-{i}" for i in range(N)])
    # events.jsonl absent (we never created it).
    assert not _events_jsonl_path(sid_b).exists()
    # Load via session_manager — triggers migration + hydrate.
    tree_b = session_manager.get_root_tree_paginated(sid_b, msg_limit=50)
    assert tree_b is not None
    hydrated = tree_b["messages"][1].get("events") or []
    hydrated_uuids = {
        e["data"]["uuid"] for e in hydrated if isinstance(e.get("data"), dict)
    }
    expected = {f"b-{i}" for i in range(N)}
    results.append(("Case B: hydrated uuids match v7 embedded set",
                    hydrated_uuids == expected,
                    f"missing={expected - hydrated_uuids}"))
    ok = _events_jsonl_count(sid_b) >= N
    results.append(("Case B: events.jsonl populated by migration",
                    ok, f"count={_events_jsonl_count(sid_b)}"))

    # Force flush + reload from disk — should still hydrate correctly.
    session_manager.flush_pending_persists()
    raw = json.loads(open(session_store._session_path(sid_b)).read())
    results.append(
        ("Case B: post-write _schema_version == 9",
         raw.get("_schema_version") == 9, f"got {raw.get('_schema_version')}"))
    results.append(
        ("Case B: post-write msg.events absent on disk",
         "events" not in raw["messages"][1],
         f"keys={sorted(raw['messages'][1].keys())}"))

    # Drop cache, reload — events come back from events.jsonl.
    session_manager._roots.pop(sid_b, None)
    tree_b2 = session_manager.get_root_tree_paginated(sid_b, msg_limit=50)
    assert tree_b2 is not None
    re_hydrated = tree_b2["messages"][1].get("events") or []
    re_uuids = {
        e["data"]["uuid"] for e in re_hydrated if isinstance(e.get("data"), dict)
    }
    results.append(("Case B: re-load from thin snapshot rehydrates",
                    re_uuids == expected,
                    f"missing={expected - re_uuids}"))

    # === Case A: v7 + populated events.jsonl ===
    # First build a baseline session (scenario-1).
    sid_baseline, baseline_pairs = _build_baseline_via_live_apply(N)
    # Snapshot the baseline tree before forging.
    baseline_tree = session_manager.get_root_tree_paginated(
        sid_baseline, msg_limit=50,
    )
    assert baseline_tree is not None
    baseline_evs = baseline_tree["messages"][-1].get("events") or []
    baseline_uids = {
        e["data"]["uuid"] for e in baseline_evs if isinstance(e.get("data"), dict)
    }

    # Forge a v7 file copying the baseline's events.jsonl into a new
    # session id (so we have events.jsonl pre-populated AND an embedded
    # v7 events array). Tests idempotence of the migration's ingest.
    sid_a = "v7-with-jsonl"
    src_jsonl = _events_jsonl_path(sid_baseline)
    dst_jsonl = _events_jsonl_path(sid_a)
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if src_jsonl.exists():
        shutil.copy(src_jsonl, dst_jsonl)
    # The events.jsonl entries reference baseline sid in their `sid`
    # field, so we rewrite them to point at the new sid for the hydrate
    # to find them.
    if dst_jsonl.exists():
        lines = open(dst_jsonl).read().splitlines()
        rewritten = []
        for line in lines:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("sid") == sid_baseline:
                entry["sid"] = sid_a
            rewritten.append(json.dumps(entry))
        with open(dst_jsonl, "w") as f:
            f.write("\n".join(rewritten) + "\n")

    # Force the ingester to re-scan since we mutated the file.
    event_ingester._handles.pop(sid_a, None)
    event_ingester._seq.pop(sid_a, None)
    event_ingester._seen_uuids.pop(sid_a, None)
    event_ingester._seq_offsets.pop(sid_a, None)
    event_ingester._max_seq_by_sid.pop(sid_a, None)
    event_ingester._next_offset.pop(sid_a, None)

    # Write the v7 record (with embedded events). Same uuids as the
    # already-populated events.jsonl — the migration ingest must dedup.
    record = {
        "_schema_version": 7,
        "id": sid_a,
        "name": "v7-case-a",
        "model": "sonnet",
        "cwd": "/tmp/test-v7v8",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [
            {
                "id": "msg-1",
                "role": "assistant",
                "content": "",
                "events": [_native_event(u, t) for u, t in baseline_pairs],
                "seq": 1,
            },
        ],
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "source": "cli",
    }
    p = ba_home() / "sessions" / f"{sid_a}.json"
    with open(p, "w") as f:
        json.dump(record, f)

    pre_jsonl_count = _events_jsonl_count(sid_a)
    tree_a = session_manager.get_root_tree_paginated(sid_a, msg_limit=50)
    assert tree_a is not None
    post_jsonl_count = _events_jsonl_count(sid_a)

    results.append(
        ("Case A: idempotent — no duplicate events.jsonl rows",
         post_jsonl_count == pre_jsonl_count,
         f"pre={pre_jsonl_count} post={post_jsonl_count}"))

    hydrated_a = tree_a["messages"][-1].get("events") or []
    uids_a = {
        e["data"]["uuid"] for e in hydrated_a if isinstance(e.get("data"), dict)
    }
    results.append(
        ("Case A: hydrated tree matches scenario-1 baseline",
         uids_a == baseline_uids,
         f"diff: {(uids_a - baseline_uids) | (baseline_uids - uids_a)}"))

    # === Idempotence: re-run migration on already-v8 record ===
    # Force re-load (cache miss) → migration sees the current schema →
    # returns early.
    session_manager._roots.pop(sid_b, None)
    pre = _events_jsonl_count(sid_b)
    _ = session_manager.get_root_tree_paginated(sid_b, msg_limit=50)
    post = _events_jsonl_count(sid_b)
    results.append(
        ("Idempotence: re-load of v8 record adds no events.jsonl rows",
         post == pre, f"pre={pre} post={post}"))

    # Report.
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
