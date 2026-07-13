"""Real-agent drain convergence test.

Drives a REAL native turn end-to-end through the full stack (uvicorn backend
in-process → ClaudeProvider → real `claude` runner → tailer →
`_watch_per_turn_complete` with the deterministic `_await_tailer_drained`
barrier) and asserts the bug the drain fix targets cannot happen:

  the latest assistant message's BYTE-RANGE render (message_event_summaries →
  read_ws_events_range → extract_output_text — the path the UI snapshot uses)
  equals the persisted msg.content, and there is NO renderable msg_id=None
  orphan for the turn.

If the per-turn `complete` fired before the tailer drained (the old fixed
sleep(0.2) race), the final assistant line would land as a msg_id=None
orphan past turn_complete → the byte-range render would be empty/wrong and
diverge from the content. With the drain barrier they converge.

Requires the `claude` CLI + a configured provider (same env as
integration_test.py). CLAUDE_CONFIG_DIR from env, else ~/.claude-zai.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import _test_home
_test_home.isolate("bc-drain-int-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
os.environ.setdefault(
    "CLAUDE_CONFIG_DIR",
    os.path.expanduser("~/.claude-zai"),
)

import httpx  # noqa: E402
import websockets  # noqa: E402
from auth_test_helpers import authenticate_async_client  # noqa: E402
from integration_test import BackgroundUvicorn, free_port  # noqa: E402

PROMPT = (
    "In exactly three sentences, explain why the sky appears blue. "
    "Write only the three sentences."
)
failures = []


def _check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}", flush=True)
    if not cond:
        failures.append(msg)


async def _wait_turn_done(client, sid, *, timeout=120.0):
    """Poll until the session ran a turn and went back to not-running."""
    loop = asyncio.get_event_loop()
    started = False
    end = loop.time() + timeout
    while loop.time() < end:
        r = await client.get("/api/sessions")
        row = next((s for s in r.json()["sessions"] if s["id"] == sid), None)
        running = bool(row and row.get("is_running"))
        if running:
            started = True
        elif started:
            return True
        await asyncio.sleep(0.5)
    return False


def _latest_assistant(sid):
    from session_manager import manager
    ref = manager.get_ref(sid)
    msgs = (ref or {}).get("messages") or []
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            return m
    return None


def _byte_range_render(sid, msg_id):
    """Reproduce the UI snapshot's render of one message from events.jsonl
    offsets — the exact path that was blind to orphans."""
    from event_ingester import event_ingester
    from event_shape import extract_output_text, strip_synthetic_events
    summ = event_ingester.message_event_summaries(sid).get(msg_id)
    if not summ:
        return None, []
    evs = event_ingester.read_ws_events_range(
        sid, summ["byte_start"], summ["byte_end"],
    )
    return extract_output_text(strip_synthetic_events(evs)), evs


async def _run():
    port = free_port()
    server = BackgroundUvicorn("main:app", port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws/chat"
    cwd = tempfile.mkdtemp(prefix="bc-drain-cwd-")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=180) as client:
            token = await authenticate_async_client(client)
            ws_url = f"{ws_url}?token={token}"
            r = await client.post("/api/sessions", json={
                "name": "DrainConvergence",
                "model": "claude-haiku-4-5-20251001",
                "cwd": cwd,
                "orchestration_mode": "native",
            })
            if r.status_code != 200:
                _check(False, f"create session HTTP {r.status_code}: {r.text}")
                return
            sid = r.json()["id"]
            print(f"  session {sid[:8]} created", flush=True)

            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe", "subscription_class": "foreground", "app_session_id": sid, "cwd": cwd,
                }))
                await asyncio.sleep(0.3)
                await ws.send(json.dumps({
                    "type": "send_message", "app_session_id": sid,
                    "prompt": PROMPT, "cwd": cwd,
                }))
                print("  prompt sent; waiting for the turn to finish...", flush=True)
                done = await _wait_turn_done(client, sid)
            _check(done, "native turn started and completed")
            if not done:
                return

            # Let the post-complete persist settle (in-process; brief).
            await asyncio.sleep(0.5)

            msg = _latest_assistant(sid)
            _check(msg is not None, "latest assistant message exists")
            if not msg:
                return
            content = (msg.get("content") or "").strip()
            _check(len(content) > 0, f"persisted msg.content non-empty ({len(content)} chars)")

            rendered, evs = _byte_range_render(sid, msg["id"])
            rendered = (rendered or "").strip()
            print(f"  persisted={len(content)} chars  byte-range-render={len(rendered)} chars",
                  flush=True)

            # THE DRAIN INVARIANT: the byte-range render path (UI snapshot)
            # reconstructs the SAME answer as the persisted content. A drain
            # race would orphan the final line → empty/divergent render.
            _check(len(rendered) > 0,
                   "byte-range render is non-empty (answer captured under msg_id, "
                   "not orphaned)")
            _check(rendered == content,
                   "byte-range render == persisted content (render paths converge)")
            if rendered != content:
                print(f"    DIVERGENCE\n    persisted: {content[:160]!r}\n"
                      f"    rendered:  {rendered[:160]!r}", flush=True)

            # No renderable msg_id=None orphan for this turn.
            from event_ingester import event_ingester
            raw, _total, _more = event_ingester.read_events(sid, limit=100000)
            orphan_text = [
                e for e in raw
                if not e.get("msg_id")
                and isinstance(e.get("data"), dict)
                and "text" in json.dumps(e.get("data"))[:400]
                and (e.get("seq") or 0) >= (msg.get("seq") or 0)
            ]
            _check(len(orphan_text) == 0,
                   f"no msg_id=None text orphan at/after the turn (found {len(orphan_text)})")
    finally:
        server.stop()


def main():
    asyncio.run(_run())
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAIL'}: "
          f"{len(failures)} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
