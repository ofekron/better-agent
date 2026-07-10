"""Regression: `get_ref` during hydration must not re-enter the load body.

Hydration (`render_tree_hydrate.hydrate_msg_events_from_jsonl`) calls
`session_manager.get_ref` at two sites (bulk_live_root check + per-msg
content re-derivation). `get_ref` → `_cached` → `_load_root`. For a root
whose on-disk fingerprint is racing the cache (an actively-written
session), that re-entrant `_load_root` saw `_cached_root_is_stale` True
→ drop → cold reload → hydrate again, and because the drop discards
`_event_hydrated_roots` while the phantom-batch guard only covers the
warm branch, the cycle repeated until RecursionError. The RecursionError
was swallowed inside `_hydrate_cached_root_events`, so hydration aborted
and `msg.events` stayed empty — the empty "No output" assistant boxes.

The fix is a same-thread re-entrancy guard at the top of `_load_root`
that short-circuits to the resident ref. This test locks that in:
during a single cold load + hydrate, the load body (`_load_root_impl`)
must never be entered re-entrantly (max concurrent depth == 1), and
`msg.events` must still be populated. Pre-fix the re-entrant warm load
runs (depth >= 2); post-fix the guard prevents it (depth == 1).
"""
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_loadroot_reentrancy_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_ingester import event_ingester  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    try:
        session = session_manager.create(
            name="reentrancy", cwd="/tmp", orchestration_mode="native",
        )
        sid = session["id"]
        msg_id = "msg-reentrancy"
        session_manager.append_assistant_msg(
            sid,
            {
                "id": msg_id,
                "role": "assistant",
                "content": "",
                "events": [],
                "timestamp": "2026-06-20T00:00:00",
                "isStreaming": False,
                "workers": [],
            },
        )
        event_ingester.ingest(
            sid,
            sid=sid,
            event_type="agent_message",
            data={
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "the real answer"}],
                },
            },
            source="reentrancy-test",
            msg_id=msg_id,
        )
        event_ingester.close_all()

        # Trace concurrent depth of the load body. A re-entrant get_ref
        # during hydrate must NOT reach `_load_root_impl` — the guard in
        # `_load_root` short-circuits it to the resident ref.
        depth = {"n": 0, "max": 0}
        _orig_impl = session_manager._load_root_impl

        def tracing_impl(*args, **kwargs):
            depth["n"] += 1
            if depth["n"] > depth["max"]:
                depth["max"] = depth["n"]
            try:
                return _orig_impl(*args, **kwargs)
            finally:
                depth["n"] -= 1

        # Force a cold load so get_ref actually runs the disk read +
        # hydrate (create/append already cached + hydrated the root with
        # the empty msg.events list).
        rid = session_manager._root_id_for(sid)
        session_manager._roots.pop(rid, None)
        session_manager._event_hydrated_roots.discard(rid)

        session_manager._load_root_impl = tracing_impl
        try:
            hydrated = session_manager.get_root_tree(sid)
            root = session_manager.get_ref(sid)
        finally:
            session_manager._load_root_impl = _orig_impl

        assert root is not None and hydrated is not None
        assert depth["max"] == 1, (
            f"re-entrant _load_root_impl reached depth {depth['max']} — "
            "get_ref during hydrate re-entered the load body (guard missing)"
        )
        msg = next(m for m in root["messages"] if m["id"] == msg_id)
        events = msg.get("events") or []
        assert len(events) > 0, (
            f"expected hydrated msg.events, got {len(events)}"
        )

        print(f"PASS: max load-body depth={depth['max']} (no re-entrant "
              "load); msg.events populated")
        return 0
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
