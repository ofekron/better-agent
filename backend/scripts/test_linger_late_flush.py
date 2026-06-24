"""Late-flush gap closure: while a babysitter runner lingers, the run
tailer stays ALIVE (tailer lifetime = process lifetime), so a CLI line
flushed AFTER complete.json is still consumed — and routed to the
orphan funnel, NOT the run queue (the turn-loop consumer broke on
`complete`, so the queue is dead post-finalize).

Uses a REAL ClaudeJsonlTailer on a temp jsonl: write one line, let the
turn finalize off complete.json (runner "process" still alive), then
append a late line and assert the tailer consumed it without enqueuing
it. Application of the late line (events.jsonl + reconcile) is locked
by test_late_flush_application.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-lateflush-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider_claude import ClaudeProvider, RunState  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _FakePopen:
    def __init__(self):
        self.pid = os.getpid()
        self._rc = None

    def poll(self):
        return self._rc


def _line(uuid_: str, text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "uuid": uuid_,
        "sessionId": "late-flush-sid",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n"


async def _scenario() -> None:
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    jsonl = run_dir / "session.jsonl"
    jsonl.write_text(_line("u1", "during-turn"))

    prov = ClaudeProvider.__new__(ClaudeProvider)
    prov._runs = {}
    prov.id = "test-prov"

    popen = _FakePopen()
    rs = RunState(
        run_id="run-lf",
        run_dir=run_dir,
        popen=popen,
        mode="native",
        app_session_id="sid-lf",
        queue=asyncio.Queue(),
        jsonl_path=jsonl,
    )
    prov._runs[rs.run_id] = rs

    # Real tailer + watcher topology, exactly like _bootstrap_run.
    prov._start_tailer_and_watchers(rs, start_offset=0)

    first = await asyncio.wait_for(rs.queue.get(), timeout=5)
    check(first.type == "agent_message", "in-turn line dispatched")

    # Turn ends; runner lingers (popen still alive).
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": "late-flush-sid", "error": None,
        "token_usage": None,
    }))
    ev = await asyncio.wait_for(rs.queue.get(), timeout=5)
    check(ev.type == "complete", "complete fired off the file while alive")
    check(prov._runs.get("run-lf") is rs, "run still registered (handoff)")

    # LATE FLUSH: the CLI writes one more line after the turn finalized.
    with open(jsonl, "a") as f:
        f.write(_line("u2", "late-flush"))
    target = jsonl.stat().st_size
    deadline = asyncio.get_event_loop().time() + 5
    while rs.processed_byte < target and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    check(rs.processed_byte >= target,
          "late post-complete line still consumed (tailer alive)")
    check(rs.queue.empty(),
          "late line routed to orphan funnel, not the dead run queue")

    # Process exits → tailer stops, run deregisters.
    popen._rc = 0
    deadline = asyncio.get_event_loop().time() + 5
    while "run-lf" in prov._runs and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    check("run-lf" not in prov._runs, "deregistered after process exit")


def main() -> int:
    asyncio.run(_scenario())
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: late-flush lines survive the linger window")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
