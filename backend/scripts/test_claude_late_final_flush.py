"""Regression: the claude CLI can flush the turn's FINAL assistant text
line to its session jsonl AFTER the runner writes complete.json
(post-Result flush — observed with claude-fable-5). A one-shot file-size
snapshot in `_await_tailer_drained` then misses the line: `complete` is
enqueued first, the turn-loop consumer breaks, and the late line falls to
the orphan funnel (events.jsonl only, never the render tree) — so
`msg.content` stays at an earlier text and `ask_team_message` grabs a
stale answer (the "lead-in only" bug).

Locks the fix: `_watch_complete` reads complete.json's
`final_assistant_text` BEFORE draining and the drain waits until the
jsonl contains that text (last matching primary assistant line) and the
tailer cursor covers it.

Pre-fix: the drain passes on the stale snapshot → `complete` is enqueued
while the final line is still unflushed → the "no complete before final
text" assertions FAIL. Post-fix: PASSES.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _test_home
_test_home.isolate("bc_late_flush_")

from provider_claude import (  # noqa: E402
    ClaudeProvider,
    RunState,
    _scan_for_final_text,
)

FINAL = "**A — final review text** the drain must wait for."

failures = []


def _check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _FakePopen:
    def __init__(self):
        self._rc = None

    def poll(self):
        return self._rc


def _assistant_line(text, *, sidechain=False, uuid="u-final"):
    entry = {
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    }
    if sidechain:
        entry["isSidechain"] = True
    return json.dumps(entry) + "\n"


def _mk_run(tmp, *, complete_payload):
    run_dir = tmp / "run"
    run_dir.mkdir()
    jsonl = tmp / "session.jsonl"
    jsonl.write_text(
        _assistant_line("lead-in text", uuid="u-lead"), encoding="utf-8",
    )
    (run_dir / "complete.json").write_text(
        json.dumps(complete_payload), encoding="utf-8",
    )
    q: asyncio.Queue = asyncio.Queue()
    rs = RunState(
        run_id="r-late", run_dir=run_dir, popen=_FakePopen(), mode="native",
        app_session_id="s-late", queue=q,
    )
    rs.jsonl_path = jsonl
    rs.processed_byte = jsonl.stat().st_size  # tailer fully caught up
    return rs, jsonl, q


async def _test_drain_waits_for_late_final_text():
    print("drain waits for a late-flushed final assistant line:")
    prov = ClaudeProvider({"id": "late-flush-prov"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_late_flush_run_"))
    rs, jsonl, _q = _mk_run(tmp, complete_payload={
        "success": True, "session_id": "cs", "final_assistant_text": FINAL,
    })

    task = asyncio.create_task(prov._await_tailer_drained(
        rs, timeout=3.0, expected_final_text=FINAL,
    ))
    await asyncio.sleep(0.3)
    _check(not task.done(),
           "drain does NOT finish while the final text line is unflushed")

    # A sidechain (subagent) line with identical text must not satisfy it.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line(FINAL, sidechain=True, uuid="u-side"))
    rs.processed_byte = jsonl.stat().st_size
    await asyncio.sleep(0.3)
    _check(not task.done(),
           "a sidechain line with the same text does not satisfy the drain")

    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line(FINAL))
    rs.processed_byte = jsonl.stat().st_size
    drained = await task
    _check(drained, "drain finishes once the primary final line is "
                    "flushed and the cursor covers it")


async def _test_final_boundary_ignores_trailing_metadata():
    print("final-text boundary ignores unrelated trailing rows:")
    prov = ClaudeProvider({"id": "late-flush-boundary-prov"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_late_flush_boundary_"))
    rs, jsonl, _q = _mk_run(tmp, complete_payload={
        "success": True, "session_id": "cs", "final_assistant_text": FINAL,
    })

    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line(FINAL))
    final_line_end = jsonl.stat().st_size
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "last-prompt", "lastPrompt": "x"}) + "\n")
        fh.write('{"type":"last-prompt"')
    rs.processed_byte = final_line_end

    loop = asyncio.get_running_loop()
    started = loop.time()
    drained = await prov._await_tailer_drained(
        rs, timeout=0.3, expected_final_text=FINAL,
    )
    elapsed = loop.time() - started
    _check(drained, "authoritative final assistant boundary completes the drain")
    _check(elapsed < 0.2,
           f"trailing complete/partial metadata does not delay completion ({elapsed:.3f}s)")


async def _test_watch_complete_orders_final_text_before_complete():
    print("_watch_complete: no `complete` before the final text line:")
    prov = ClaudeProvider({"id": "late-flush-prov-2"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_late_flush_wc_"))
    rs, jsonl, q = _mk_run(tmp, complete_payload={
        "success": True, "session_id": "cs", "final_assistant_text": FINAL,
    })
    prov._runs[rs.run_id] = rs

    watch = asyncio.create_task(prov._watch_complete(rs))
    await asyncio.sleep(0.4)
    _check(q.empty(),
           "complete is NOT enqueued while the final line is unflushed")

    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write(_assistant_line(FINAL))
    rs.processed_byte = jsonl.stat().st_size
    await watch
    ev = q.get_nowait()
    _check(ev.type == "complete" and ev.data.get("success") is True,
           "complete is enqueued after the final line is covered")

    rs.popen._rc = 0  # let the wind-down watcher exit and clean up
    if rs.complete_task is not None:
        await rs.complete_task


async def _test_guard_skipped_without_final_text():
    print("guard skipped when complete.json carries no final text:")
    prov = ClaudeProvider({"id": "late-flush-prov-3"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_late_flush_skip_"))
    rs, _jsonl, q = _mk_run(tmp, complete_payload={
        "success": False, "session_id": "cs", "error": "boom",
        "final_assistant_text": None,
    })
    prov._runs[rs.run_id] = rs

    loop = asyncio.get_running_loop()
    started = loop.time()
    watch = asyncio.create_task(prov._watch_complete(rs))
    await watch
    elapsed = loop.time() - started
    ev = q.get_nowait()
    _check(ev.type == "complete" and ev.data.get("success") is False,
           "failed turn completes without a final-text wait")
    _check(elapsed < 2.0, f"no drain-timeout stall (took {elapsed:.2f}s)")

    rs.popen._rc = 0
    if rs.complete_task is not None:
        await rs.complete_task


def _test_scan_for_final_text():
    print("_scan_for_final_text semantics:")
    tmp = Path(tempfile.mkdtemp(prefix="bc_late_flush_scan_"))
    p = tmp / "scan.jsonl"
    early = _assistant_line(FINAL, uuid="u-early")
    side = _assistant_line(FINAL, sidechain=True, uuid="u-side")
    final = _assistant_line(FINAL, uuid="u-final")
    p.write_text(early + side + final, encoding="utf-8")

    end, nxt = _scan_for_final_text(p, 0, FINAL)
    _check(end == len(early) + len(side) + len(final),
           "last matching PRIMARY line wins (not the earlier duplicate)")
    _check(nxt == p.stat().st_size, "scan offset advances to EOF")

    end, _ = _scan_for_final_text(p, 0, "not present")
    _check(end is None, "no match returns None")

    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant"')  # partial line, no newline
    end, nxt = _scan_for_final_text(p, nxt, FINAL)
    _check(end is None and nxt < p.stat().st_size,
           "partial trailing line is left for the next scan")


def main():
    asyncio.run(_test_drain_waits_for_late_final_text())
    asyncio.run(_test_final_boundary_ignores_trailing_metadata())
    asyncio.run(_test_watch_complete_orders_final_text_before_complete())
    asyncio.run(_test_guard_skipped_without_final_text())
    _test_scan_for_final_text()
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
