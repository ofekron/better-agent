"""Isolates the MIDDLE layer of the native pipeline's provider-content
chain — the piece neither of its two sibling regression tests covers:

  - `scripts/test_claude_jsonl_tailer_real_line_dispatch.py` proves
    `jsonl_tailer.ClaudeJsonlTailer` itself dispatches a freshly-appended
    real assistant line to ITS OWN `dispatch` callback.
  - `scripts/test_v2_projection_end_to_end.py::test_real_dispatch_
    through_turn_manager_run_turn_journals_provider_stream_content`
    proves `TurnManager._drive_cli_run`'s `queue.get()` loop ->
    `save_ws_callback` -> `_publish_provider_stream_event` correctly
    journals a `StreamEvent` that's ALREADY sitting on the queue.

Neither exercises `provider_claude.py::ClaudeProvider._start_tailer_and_
watchers` / `_dispatch_tailer_line` — the GLUE that wires the tailer's
`dispatch` callback to `rs.queue.put_nowait(StreamEvent(...))`. This
test drives that glue directly: a real `ClaudeProvider` + `RunState`,
`_start_tailer_and_watchers` started for real (spawning the real
`ClaudeJsonlTailer`), a real assistant line appended to the tailed
file mid-run (turn NOT finalized), and asserts the resulting
`StreamEvent` lands on `rs.queue` with the exact shape `_drive_cli_run`
expects (`type="agent_message"`, `data` = the raw enriched claude line).

No live claude CLI process is spawned or required.
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
_test_home.isolate("bc-test-provider-claude-tailer-glue-")

from provider_claude import ClaudeProvider, RunState  # noqa: E402


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    assert ok, name


def test_tailed_assistant_line_reaches_run_state_queue() -> None:
    print("T1 _start_tailer_and_watchers glue puts a real assistant line onto rs.queue")
    tmp = Path(tempfile.mkdtemp(prefix="bc-provider-claude-tailer-glue-run-"))
    try:
        run_dir = tmp / "run"
        run_dir.mkdir()
        jsonl_path = tmp / "session.jsonl"
        jsonl_path.write_text("", encoding="utf-8")

        provider = ClaudeProvider({"id": "glue-test-prov"})
        queue: asyncio.Queue = asyncio.Queue()
        rs = RunState(
            run_id="glue-run-1", run_dir=run_dir, popen=object(),
            mode="native", app_session_id="glue-sess-1", queue=queue,
        )
        rs.jsonl_path = jsonl_path
        assistant_uuid = str(uuid.uuid4())

        async def _drive() -> None:
            provider._start_tailer_and_watchers(rs, 0)
            try:
                await asyncio.sleep(0.3)  # let the real tail-F follower attach

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

                event = await asyncio.wait_for(queue.get(), timeout=10.0)
                return event
            finally:
                if rs.tailer is not None:
                    rs.tailer.stop()
                for task in (rs.tailer_task, rs.complete_task):
                    if task is not None:
                        task.cancel()
                for task in (rs.tailer_task, rs.complete_task):
                    if task is not None:
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

        event = asyncio.run(_drive())

        check("an event reached rs.queue before timeout", event is not None)
        check("event type is agent_message", event.type == "agent_message")
        check("event data is the raw enriched assistant line", event.data.get("type") == "assistant")
        check("event data.uuid matches the appended line", event.data.get("uuid") == assistant_uuid)
        content = (event.data.get("message") or {}).get("content") or [{}]
        check("event data carries the real assistant text", content[0].get("text") == "PONG")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_TESTS = [
    test_tailed_assistant_line_reaches_run_state_queue,
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
