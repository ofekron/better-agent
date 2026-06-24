"""Locks the v8 worker-panel round-trip: a manager session driving
`worker_event` frames through `apply_event` writes them to events.jsonl
with `msg_id=parent_msg_id, sid=parent_sid`. On cold-load, hydration
rebuilds `msg.workers[delegation_id].events` via
`apply_worker_panel_event` — inner events identical to pre-write.

This is the highest-risk path because it crosses two namespaces:
  - events.jsonl entry: outer `worker_event` wrapper.
  - msg.workers[panel].events entry: inner agent_message (unwrapped).

Run with:
    cd backend && .venv/bin/python scripts/test_worker_panel_round_trip.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-panel-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _agent_msg_event(uuid: str, text: str) -> dict:
    """Inner agent_message frame — what shows up on panel.events."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _worker_event(delegation_id: str, inner: dict) -> dict:
    """Outer worker_event wrapper — what shows up on events.jsonl."""
    return {
        "type": "worker_event",
        "data": {
            "delegation_id": delegation_id,
            "event": inner,
        },
    }


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    # Build a manager-mode session with a worker panel populated by
    # firing worker_event through apply_event(source_is_provider_stream=True).
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/panel-rt",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy("manager")
    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "msg-1"
    scaffold["role"] = "assistant"
    scaffold["seq"] = 1
    scaffold["workers"] = [
        {
            "delegation_id": "del-A",
            "name": "worker-A",
            "status": "running",
            "events": [],
        },
        {
            "delegation_id": "del-B",
            "name": "worker-B",
            "status": "running",
            "events": [],
        },
    ]
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid, run_id="run-panel")

    # Fire 5 worker_event into panel A, 3 into panel B.
    panel_a_pairs = [(f"A-{i}", f"a-text-{i}") for i in range(5)]
    panel_b_pairs = [(f"B-{i}", f"b-text-{i}") for i in range(3)]
    for u, t in panel_a_pairs:
        strategy.apply_event(
            app_session_id=sid, msg=msg,
            event=_worker_event("del-A", _agent_msg_event(u, t)),
            ctx=ctx, source_is_provider_stream=True,
        )
    for u, t in panel_b_pairs:
        strategy.apply_event(
            app_session_id=sid, msg=msg,
            event=_worker_event("del-B", _agent_msg_event(u, t)),
            ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.flush_pending_persists()

    # 1) Live: panel.events populated with INNER frames.
    pre = session_manager.get_ref(sid)["messages"][-1]
    panels_pre = {p["delegation_id"]: p for p in (pre.get("workers") or [])}
    a_uids_pre = [
        e["data"]["uuid"] for e in panels_pre["del-A"]["events"]
        if isinstance(e.get("data"), dict)
    ]
    b_uids_pre = [
        e["data"]["uuid"] for e in panels_pre["del-B"]["events"]
        if isinstance(e.get("data"), dict)
    ]
    results.append(("live: panel A has 5 inner events",
                    len(a_uids_pre) == 5, f"got {len(a_uids_pre)}"))
    results.append(("live: panel B has 3 inner events",
                    len(b_uids_pre) == 3, f"got {len(b_uids_pre)}"))

    # 2) On-disk: panel.events fields omitted.
    on_disk = json.loads(open(session_store._session_path(sid)).read())
    disk_panels = {p["delegation_id"]: p for p in on_disk["messages"][-1].get("workers", [])}
    results.append(
        ("on-disk: panel A events absent",
         "events" not in disk_panels["del-A"],
         f"keys={sorted(disk_panels['del-A'].keys())}"))
    results.append(
        ("on-disk: panel B events absent",
         "events" not in disk_panels["del-B"],
         f"keys={sorted(disk_panels['del-B'].keys())}"))

    # 3) events.jsonl: outer worker_event entries are there.
    raw, _, _ = event_ingester.read_events(sid, limit=20_000, sid_filter=sid)
    worker_evs = [e for e in raw if e.get("type") == "worker_event"]
    results.append(
        ("events.jsonl has 8 worker_event entries",
         len(worker_evs) == 8, f"got {len(worker_evs)}"))

    # 4) Cold-load hydration: panel.events restored from events.jsonl.
    session_manager._roots.pop(sid, None)
    tree = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    assert tree is not None
    post = tree["messages"][-1]
    panels_post = {p["delegation_id"]: p for p in (post.get("workers") or [])}
    a_uids_post = [
        e["data"]["uuid"] for e in panels_post["del-A"]["events"]
        if isinstance(e.get("data"), dict)
    ]
    b_uids_post = [
        e["data"]["uuid"] for e in panels_post["del-B"]["events"]
        if isinstance(e.get("data"), dict)
    ]
    results.append(
        ("hydrated: panel A uids match pre-write",
         set(a_uids_post) == set(a_uids_pre),
         f"diff={set(a_uids_pre) ^ set(a_uids_post)}"))
    results.append(
        ("hydrated: panel B uids match pre-write",
         set(b_uids_post) == set(b_uids_pre),
         f"diff={set(b_uids_pre) ^ set(b_uids_post)}"))

    # 5) Cross-panel attribution: no leak between A and B.
    results.append(
        ("hydrated: no leak from panel A to panel B",
         not (set(a_uids_post) & set(b_uids_pre))
         and not (set(b_uids_post) & set(a_uids_pre)),
         "cross-panel leak detected"))

    # 6) Inner event data byte-identical for one sampled uuid.
    target_uid = a_uids_pre[0]
    pre_data = next(
        e["data"] for e in panels_pre["del-A"]["events"]
        if isinstance(e.get("data"), dict) and e["data"].get("uuid") == target_uid
    )
    post_data = next(
        e["data"] for e in panels_post["del-A"]["events"]
        if isinstance(e.get("data"), dict) and e["data"].get("uuid") == target_uid
    )
    results.append(
        ("hydrated: panel inner data byte-identical for sampled uuid",
         pre_data == post_data, "data diverged after hydrate"))

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
