"""Regression: a mid-turn hard refresh must not drop the streaming turn.

The REST snapshot hands the frontend a per-sid watermark
(`max_seq_by_sid` on the tree) that the WS subscribe path resumes from
via `events_from_seq`. That watermark MUST equal the highest seq
materialized into the *rendered* snapshot — the render-projection head —
NOT the raw journal head.

Bug: the handler stamped the raw head (`event_journal_reader.
max_seq_by_context` → `event_ingester.max_seq_by_sid`), which counts
trailing non-render / not-yet-resolved events too. Mid-turn those
trailing events pushed the watermark PAST the rendered tail, so the WS
resume drained `(raw_head, …]` and the still-streaming turn was never
redelivered — it vanished until the turn finished and reconcile rebuilt
the message summary.

Pins:
  1. render_seq_by_sid == render head, while max_seq_by_sid == raw head,
     when a non-render event trails the last render event.
  2. GET /api/sessions watermark (tree["max_seq_by_sid"]) == render head.
  3. _floor_events_from_seq (WS cursor floor, cursor_known=False) ==
     render head, not raw head.

Run with:
    cd backend && .venv/bin/python scripts/test_streaming_refresh_watermark.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-refresh-wm-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402
import event_ingester as ei_mod  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
import main as main_mod  # noqa: E402

event_ingester = ei_mod.event_ingester

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _fresh_streaming_session() -> tuple[str, int]:
    """Create a session whose latest assistant msg is still streaming,
    ingest one render event for it, then ingest a trailing NON-render
    event for the same sid. Returns (sid, render_head_seq)."""
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    msg_id = str(uuid.uuid4())
    session_manager.append_assistant_msg(sid, {
        "id": msg_id, "role": "assistant", "content": "",
        "events": [], "isStreaming": True,
    })
    # A render-affecting event for the streaming message.
    event_ingester.ingest(
        sid, sid=sid, event_type="agent_message",
        data={"type": "assistant", "uuid": str(uuid.uuid4())},
        source="test", msg_id=msg_id,
    )
    render_head = event_ingester.cursor(sid)
    # A trailing NON-render event (metadata / lifecycle) for the same sid.
    event_ingester.ingest(
        sid, sid=sid, event_type="run_state",
        data={"uuid": str(uuid.uuid4()), "running": True},
        source="test", msg_id=None,
    )
    return sid, render_head


def test_projection_heads_diverge() -> bool:
    sid, render_head = _fresh_streaming_session()
    raw = event_ingester.max_seq_by_sid(sid).get(sid, 0)
    render = event_ingester.render_seq_by_sid(sid).get(sid, 0)
    if raw <= render:
        print(f"  raw head ({raw}) should exceed render head ({render})")
        return False
    if render != render_head:
        print(f"  render head {render} != expected {render_head}")
        return False
    if event_journal_reader.render_seq_by_context(sid).get(sid, 0) != render:
        print("  reader render_seq_by_context disagrees with ingester")
        return False
    return True


def test_get_session_watermark_is_render_head() -> bool:
    sid, render_head = _fresh_streaming_session()
    tree = asyncio.run(main_mod.get_session(sid, msg_limit=50, exchange_count=None))
    if hasattr(tree, "body"):
        tree = json.loads(tree.body)
    wm = (tree.get("max_seq_by_sid") or {}).get(sid, 0)
    raw = event_ingester.max_seq_by_sid(sid).get(sid, 0)
    if wm == raw:
        print(f"  watermark stamped RAW head {raw} (the bug)")
        return False
    if wm != render_head:
        print(f"  watermark {wm} != render head {render_head}")
        return False
    return True


def test_floor_events_from_seq_is_render_head() -> bool:
    sid, render_head = _fresh_streaming_session()
    floor = main_mod._floor_events_from_seq(
        sid, 0, cursor_known=False,
    )
    if floor != render_head:
        raw = event_ingester.max_seq_by_sid(sid).get(sid, 0)
        print(f"  floor {floor} != render head {render_head} (raw={raw})")
        return False
    return True


def main() -> int:
    tests = [
        ("render vs raw head diverge", test_projection_heads_diverge),
        ("GET watermark == render head", test_get_session_watermark_is_render_head),
        ("WS cursor floor == render head", test_floor_events_from_seq_is_render_head),
    ]
    fails = 0
    try:
        for name, fn in tests:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                fails += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if fails == 0:
        print(f"{PASS}  streaming-refresh watermark invariants hold")
        return 0
    print(f"{FAIL}  {fails} regression(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
