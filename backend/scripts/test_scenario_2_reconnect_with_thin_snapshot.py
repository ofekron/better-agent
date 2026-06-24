"""Locks the CLAUDE.md scenario-2 convergence invariant under v8:

Client offline, backend online, a turn streams through. The
in-memory cache accumulates msg.events; the disk snapshot (debounced)
gets thin-written without events. When the client reconnects, both:

  - `messages_replay` (built from `session_manager.get(sid)` / the
    in-memory ref the orchestrator hands the WS subscribe path), AND
  - A fresh `GET /api/sessions/{id}` after the cache is dropped (built
    from disk → migrate → hydrate from events.jsonl)

must produce the SAME `msg.events` content per (uuid, data).

Run with:
    cd backend && .venv/bin/python scripts/test_scenario_2_reconnect_with_thin_snapshot.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sc2-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
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


def _signature(events: list[dict]) -> list[tuple]:
    """Order-independent signature: sorted (uuid, json(data)) tuples."""
    out = []
    for e in events or []:
        d = e.get("data") if isinstance(e, dict) else None
        if not isinstance(d, dict):
            continue
        u = d.get("uuid")
        if u is None:
            continue
        out.append((u, json.dumps(d, sort_keys=True)))
    out.sort()
    return out


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    # Create a fresh session. Client connects, drives a turn,
    # disconnects mid-turn (simulated by not maintaining a WS — the
    # backend keeps applying events).
    sess = session_manager.create(
        name="sc2", model="sonnet", cwd="/tmp/sc2",
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
    ctx = ApplyEventCtx(root_id=sid, run_id="run-sc2")

    # Stream 25 events through apply_event(source_is_provider_stream=True). The debounce
    # may queue some writes; we mix in an explicit flush at the end
    # to materialize the thin disk snapshot.
    for i in range(25):
        ev = _native_event(f"u-{i}", f"text-{i}")
        strategy.apply_event(
            app_session_id=sid, msg=msg, event=ev, ctx=ctx, source_is_provider_stream=True,
        )
    session_manager.flush_pending_persists()

    # === Path 1: messages_replay built from session_manager.get(sid) ===
    # (mirrors the orchestrator's catch-up path in main.py:3279-3308)
    live_sess = session_manager.get(sid)
    assert live_sess is not None
    replay_msg = live_sess["messages"][-1]
    replay_sig = _signature(replay_msg.get("events"))

    # === Path 2: fresh REST GET after cache drop (cold-load) ===
    session_manager._roots.pop(sid, None)
    rest_tree = session_manager.get_root_tree_paginated(sid, msg_limit=50)
    assert rest_tree is not None
    rest_msg = rest_tree["messages"][-1]
    rest_sig = _signature(rest_msg.get("events"))

    # 1) Both paths see 25 events.
    results.append(("live cache replay path: 25 events",
                    len(replay_sig) == 25, f"got {len(replay_sig)}"))
    results.append(("cold-load REST path: 25 events",
                    len(rest_sig) == 25, f"got {len(rest_sig)}"))

    # 2) Both paths produce IDENTICAL (uuid, data) signatures.
    results.append(("scenarios 1 vs 2-rejoin produce identical render trees",
                    replay_sig == rest_sig,
                    f"diff len={len(set(replay_sig) ^ set(rest_sig))}"))

    # 3) The set of uuids matches the events we ingested.
    expected_uids = {f"u-{i}" for i in range(25)}
    got_uids = {u for u, _ in rest_sig}
    results.append(("cold-load rebuild covers every uuid streamed",
                    got_uids == expected_uids,
                    f"missing={expected_uids - got_uids}"))

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
