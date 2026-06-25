"""Regression: runner-owned event streams (session_events.jsonl written
by the Gemini/OpenAI-family runners) must be fully drained by the tailer
BEFORE `complete` is enqueued.

Both `GeminiProvider._watch_complete` (base of Copilot/Amp/Cursor/Kimi/
pi/Qwen/OpenCode/Agy) and `OpenAIProvider._watch_complete` used a fixed
`sleep(0.2)` drain guess: when the poll tailer lagged more than 0.2s,
`complete` overtook trailing event lines — the turn loop broke, the
lines never reached the render tree, and waiters (`ask_team_message`)
grabbed stale content. Same bug class as the claude late-flush fix
(test_claude_late_final_flush.py); here a deterministic line-cursor
drain suffices because the runner appends every event line before it
writes complete.json.

`CodexProvider._watch_complete` had the identical `sleep(0.2)` gap —
missed when the line-cursor fix above was ported to Gemini/OpenAI,
since Codex is a standalone class, not an OpenAIProvider subclass.
Codex tails an externally-owned file (the CLI's own rollout jsonl) by
BYTE offset rather than line count, so it exercises
`await_line_tailer_drained(count_fn=_file_byte_size)` instead of the
line-count default.

Pre-fix: `complete` is enqueued ~0.2s after complete.json regardless of
the cursor → the "no complete while behind" assertions FAIL.
Post-fix: `complete` waits for the cursor → PASSES.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _test_home
_test_home.isolate("bc_stream_drain_")

from provider import await_line_tailer_drained  # noqa: E402

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


def _mk_run(provider_mod, tmp):
    run_dir = tmp / "run"
    run_dir.mkdir()
    events = run_dir / "session_events.jsonl"
    events.write_text(
        "".join(json.dumps({"type": "assistant", "uuid": f"u{i}"}) + "\n"
                for i in range(6)),
        encoding="utf-8",
    )
    (run_dir / "complete.json").write_text(
        json.dumps({"success": True, "session_id": "cs"}), encoding="utf-8",
    )
    q: asyncio.Queue = asyncio.Queue()
    rs = provider_mod.RunState(
        run_id="r-drain", run_dir=run_dir, popen=_FakePopen(), mode="native",
        app_session_id="s-drain", queue=q,
    )
    rs.processed_line = 2  # tailer BEHIND: 2 of 6 lines dispatched
    return rs, q


async def _watch_complete_waits(provider_cls_name):
    print(f"{provider_cls_name}._watch_complete drains before complete:")
    if provider_cls_name == "GeminiProvider":
        import provider_gemini as mod
        prov = mod.GeminiProvider({"id": "drain-gem"})
    else:
        import provider_openai as mod
        prov = mod.OpenAIProvider({"id": "drain-oai"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_stream_drain_"))
    rs, q = _mk_run(mod, tmp)
    prov._runs[rs.run_id] = rs

    watch = asyncio.create_task(prov._watch_complete(rs))
    await asyncio.sleep(0.5)
    _check(q.empty(),
           "complete is NOT enqueued while the line cursor is behind")

    rs.processed_line = 6
    await watch
    ev = q.get_nowait()
    _check(ev.type == "complete" and ev.data.get("success") is True,
           "complete is enqueued once the cursor covers the file")


def _mk_codex_run(provider_mod, tmp):
    run_dir = tmp / "run"
    run_dir.mkdir()
    rollout = tmp / "rollout.jsonl"
    rollout.write_text(
        "".join(json.dumps({"type": "event_msg", "payload": {"i": i}}) + "\n"
                for i in range(6)),
        encoding="utf-8",
    )
    (run_dir / "complete.json").write_text(
        json.dumps({"success": True, "session_id": "cs"}), encoding="utf-8",
    )
    q: asyncio.Queue = asyncio.Queue()
    rs = provider_mod.RunState(
        run_id="r-drain-codex", run_dir=run_dir, popen=_FakePopen(), mode="native",
        app_session_id="s-drain-codex", queue=q,
    )
    rs.jsonl_path = rollout
    rs.processed_byte_offset = 0  # tailer BEHIND: nothing dispatched yet
    return rs, q, rollout


async def _watch_complete_waits_codex():
    print("CodexProvider._watch_complete drains before complete (byte cursor):")
    import provider_codex as mod
    prov = mod.CodexProvider({"id": "drain-codex"})
    tmp = Path(tempfile.mkdtemp(prefix="bc_stream_drain_"))
    rs, q, rollout = _mk_codex_run(mod, tmp)
    prov._runs[rs.run_id] = rs

    watch = asyncio.create_task(prov._watch_complete(rs))
    await asyncio.sleep(0.5)
    _check(q.empty(),
           "complete is NOT enqueued while the byte cursor is behind")

    rs.processed_byte_offset = rollout.stat().st_size
    await watch
    ev = q.get_nowait()
    _check(ev.type == "complete" and ev.data.get("success") is True,
           "complete is enqueued once the byte cursor covers the file")


async def _drain_timeout_is_bounded():
    print("await_line_tailer_drained timeout is bounded:")
    tmp = Path(tempfile.mkdtemp(prefix="bc_stream_drain_to_"))
    events = tmp / "session_events.jsonl"
    events.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    loop = asyncio.get_running_loop()
    started = loop.time()
    drained = await await_line_tailer_drained(
        path=events, get_cursor=lambda: 0, run_id="r-to", timeout=0.3,
    )
    elapsed = loop.time() - started
    _check(drained is False, "wedged cursor returns False (degraded fire)")
    _check(0.25 <= elapsed < 1.5, f"timeout bounded (took {elapsed:.2f}s)")

    drained = await await_line_tailer_drained(
        path=tmp / "missing.jsonl", get_cursor=lambda: 0, run_id="r-miss",
    )
    _check(drained is True, "missing file drains immediately (target 0)")


def main():
    asyncio.run(_watch_complete_waits("GeminiProvider"))
    asyncio.run(_watch_complete_waits("OpenAIProvider"))
    asyncio.run(_watch_complete_waits_codex())
    asyncio.run(_drain_timeout_is_bounded())
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
