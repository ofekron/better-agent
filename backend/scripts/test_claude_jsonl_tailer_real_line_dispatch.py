"""Isolates the LOWEST layer of the native pipeline's provider-content
chain: does `jsonl_tailer.ClaudeJsonlTailer` — the `tail -F`-based
follower `provider_claude.py`'s `_start_tailer_and_watchers` spawns for
every native/manager CLI run — actually dispatch a real claude CLI
`type: "assistant"` jsonl line the instant it's appended to the tailed
file?

Companion to `scripts/test_v2_projection_end_to_end.py::test_real_
dispatch_through_turn_manager_run_turn_journals_provider_stream_content`,
which proves the funnel DOWNSTREAM of the queue (`_drive_cli_run`'s
`queue.get()` loop -> `save_ws_callback` -> `_publish_provider_stream_
event` -> journal) is correct given a `StreamEvent` already sitting on
the queue. This test proves (or disproves) the layer BEFORE that: does a
real assistant line landing on disk actually turn into that
`StreamEvent` in the first place? No live claude CLI process is
spawned — a synthetic jsonl file is appended to directly, exactly
mirroring what the real CLI subprocess does (an append-only jsonl,
one JSON object per line).

Run with:
    cd backend && .venv/bin/python scripts/test_claude_jsonl_tailer_real_line_dispatch.py
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-claude-tailer-dispatch-")

from jsonl_tailer import ClaudeJsonlTailer  # noqa: E402


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    assert ok, name


def test_appended_assistant_line_is_dispatched_promptly() -> None:
    print("T1 ClaudeJsonlTailer dispatches a freshly-appended assistant line")
    tmp_dir = Path(tempfile.mkdtemp(prefix="ba-tailer-probe-"))
    try:
        jsonl_path = tmp_dir / "session.jsonl"
        jsonl_path.write_text("", encoding="utf-8")

        dispatched: list[dict] = []

        async def _dispatch(enriched: dict) -> None:
            dispatched.append(enriched)

        async def _drive() -> None:
            tailer = ClaudeJsonlTailer(path=jsonl_path, start_offset=0, dispatch=_dispatch)
            task = asyncio.create_task(tailer.run())
            try:
                # Let the tail-F follower actually attach before writing —
                # the follower polls/tail -F retries, so this is generous
                # slack, not a race-masking sleep (the assertion below
                # bounds the wait deterministically via polling, not this).
                await asyncio.sleep(0.3)

                assistant_uuid = str(uuid.uuid4())
                line = json.dumps({
                    "type": "assistant",
                    "uuid": assistant_uuid,
                    "session_id": str(uuid.uuid4()),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "PONG"}],
                    },
                })
                with jsonl_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())

                deadline = asyncio.get_running_loop().time() + 10.0
                while not dispatched and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
            finally:
                tailer.stop()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    pass

        asyncio.run(_drive())

        check("exactly one line dispatched", len(dispatched) == 1)
        enriched = dispatched[0] if dispatched else {}
        check("dispatched shape is the raw assistant line", enriched.get("type") == "assistant")
        content = ((enriched.get("message") or {}).get("content") or [{}])
        text = content[0].get("text") if content else None
        check("dispatched text is 'PONG'", text == "PONG")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


_TESTS = [
    test_appended_assistant_line_is_dispatched_promptly,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
