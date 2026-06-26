"""Regression test: runner_agy MUST stream agy steps into
session_events.jsonl DURING the run, not only after the agy process exits.

Reproduces the incident behind session d9d160af-… (model "Gemini 3.5 Flash
(Medium)", which maps to the agy/antigravity runner): the agy turn ran for
10+ minutes (pid 30042 alive, agy CLI actively calling streamGenerateContent)
yet the assistant bubble stayed empty and stuck "streaming" forever, with no
error and no turn_complete. Root cause: runner_agy._run called
`proc.communicate()` (blocks until agy exits) and only wrote
session_events.jsonl AFTER exit — so the provider's polling GeminiJsonlTailer
had nothing to tail during the (long, agentic) turn, and if agy never exited
the file was never written at all.

Pre-fix code fails assertion (1): session_events.jsonl is empty while the
fake agy is still running. Post-fix code streams steps as they land, so (1)
passes, and the final flush still writes the terminal assistant message (2)
without duplicating streamed events (3).

Run with:
    cd backend && .venv/bin/python scripts/test_runner_agy_streams_events.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing any backend module.
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-agy-stream-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# agy_home must be isolated too — the runner reads the conversation DB from
# HOME/.gemini/antigravity-cli/conversations/<sid>.db and the fake agy stub
# writes new steps there mid-run.
_AGY_HOME = tempfile.mkdtemp(prefix="bc-test-agy-home-")
os.environ["HOME"] = _AGY_HOME

import runner_agy  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

# A step string that _extract_parent_subagent_events turns into a worker event.
# Distinct senders per step so each step yields its own worker events.
_SENDER_A = "11111111-2222-3333-4444-555555555555"
_SENDER_B = "66666666-7777-8888-9999-aaaaaaaaaaaa"
_CONVERSATION_ID = "deadbeef-0000-0000-0000-000000000001"


def _message_step_string(sender: str, content: str) -> str:
    return (
        f"[Message] timestamp=2026-06-24T12:00:00Z sender={sender} "
        f"priority=1 content={content}"
    )


def _create_conversation_db(step_zero_content: str) -> Path:
    db_path = runner_agy._conversation_db(Path(_AGY_HOME), _CONVERSATION_ID)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(
        "CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL "
        "DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` "
        "numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,"
        "`permissions` blob,`task_details` blob,`render_info` blob,"
        "`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,"
        "PRIMARY KEY (`idx`));"
    )
    con.execute(
        "INSERT INTO steps (idx, step_type, status, has_subtrajectory, metadata) "
        "VALUES (0, 15, 3, 0, ?)",
        (_message_step_string(_SENDER_A, "step zero hello").encode("utf-8"),),
    )
    con.commit()
    con.close()
    return db_path


def _write_fake_agy_stub(db_path: Path) -> Path:
    """A fake `agy` that adds a second step mid-run then exits 0.

    It inserts step idx=1 after a short delay so the streaming watcher has a
    chance to poll, then sleeps long enough to prove streaming happened BEFORE
    exit, then prints final stdout and exits 0.
    """
    stub_dir = tempfile.mkdtemp(prefix="bc-test-agy-stub-")
    stub_path = Path(stub_dir) / "agy"
    db_str = str(db_path)
    sender_b = _SENDER_B
    payload = json.dumps(
        f"[Message] timestamp=2026-06-24T12:00:01Z sender={sender_b} "
        f"priority=1 content=step one world"
    )
    stub_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sqlite3, sys, time\n"
        f"DB = {db_str!r}\n"
        f"PAYLOAD = {payload!r}\n"
        "time.sleep(0.3)\n"
        "con = sqlite3.connect(DB, timeout=5)\n"
        "con.execute('INSERT INTO steps (idx, step_type, status, "
        "has_subtrajectory, metadata) VALUES (1, 15, 3, 0, ?)', "
        "(json.loads(PAYLOAD).encode('utf-8'),))\n"
        "con.commit()\n"
        "con.close()\n"
        "time.sleep(1.2)\n"
        "sys.stdout.write('final agy answer')\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n"
    )
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_path


async def main() -> int:
    db_path = _create_conversation_db("step zero hello")
    stub_path = _write_fake_agy_stub(db_path)
    original_resolve = runner_agy.resolve_cli_binary

    def _fake_resolve(name: str):
        return stub_path if name == "agy" else original_resolve(name)

    runner_agy.resolve_cli_binary = _fake_resolve  # type: ignore

    run_dir = Path(tempfile.mkdtemp(prefix="bc-test-agy-run-"))
    inputs = {
        "prompt": "hi",
        "cwd": _AGY_HOME,
        "model": "fake-agy",
        "session_id": _CONVERSATION_ID,
        "app_session_id": "test-app-session",
        "mode": "native",
    }
    events_path = run_dir / "session_events.jsonl"

    failures = 0
    try:
        run_task = asyncio.create_task(runner_agy._run(run_dir, inputs))

        # (1) Streaming contract: session_events.jsonl must contain events
        # WHILE the agy process is still running (before _run returns).
        # Give the watcher (~0.5s poll) and the stub's mid-run insert time.
        streamed_lines: list[str] = []
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and not run_task.done():
            if events_path.is_file():
                streamed_lines = [
                    ln for ln in events_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                if streamed_lines:
                    break
            await asyncio.sleep(0.15)

        if run_task.done():
            print(f"{FAIL}  _run returned before we could observe mid-run streaming")
            failures += 1
        elif streamed_lines:
            print(f"{PASS}  session_events.jsonl streamed {len(streamed_lines)} "
                  f"event(s) during the run")
        else:
            print(f"{FAIL}  session_events.jsonl empty during run — events only "
                  f"written post-exit (the bug)")
            failures += 1

        rc = await run_task

        # (2) Terminal assistant message present after exit + success.
        complete_path = run_dir / "complete.json"
        complete = json.loads(complete_path.read_text()) if complete_path.is_file() else {}
        if complete.get("success") is True and rc == 0:
            print(f"{PASS}  complete.json success + rc={rc}")
        else:
            print(f"{FAIL}  complete.json={complete} rc={rc}")
            failures += 1

        final_lines = [
            ln for ln in events_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        # The terminal assistant message is agy's clean print-mode stdout,
        # replacing the empty placeholder via the shared per-run main uuid.
        agent_msgs = []
        for ln in final_lines:
            ev = json.loads(ln)
            if ev.get("type") == "agent_message":
                c = ev["data"]["message"]["content"][0]
                agent_msgs.append((ev["data"]["uuid"], c.get("text", "") if isinstance(c, dict) else ""))
        non_empty = [t for _, t in agent_msgs if t]
        if non_empty and non_empty[-1] == "final agy answer":
            print(f"{PASS}  clean stdout answer emitted ('final agy answer')")
        else:
            print(f"{FAIL}  terminal text should be clean stdout 'final agy answer', "
                  f"got {non_empty!r}")
            failures += 1

        # (3) Idempotency: the ONLY uuid allowed to repeat is the placeholder →
        # stdout replace pair (same per-run uuid; apply_event replaces by uuid
        # in the render tree). Every other uuid must be unique.
        from collections import Counter
        main_uuid = agent_msgs[0][0] if agent_msgs else None
        counts = Counter(u for u, _ in agent_msgs)
        # worker/subagent uuids also appear once each; check the whole file.
        all_uuids = []
        for ln in final_lines:
            ev = json.loads(ln)
            uid = runner_agy._event_uuid_holder(ev)
            if uid and uid.get("uuid"):
                all_uuids.append(uid["uuid"])
        counts = Counter(all_uuids)
        bad_dupes = {u: c for u, c in counts.items() if c > 1 and u != main_uuid}
        main_count = counts.get(main_uuid, 0)
        if not bad_dupes and main_count <= 2:
            print(f"{PASS}  no unintended duplicate event uuids ({len(all_uuids)} uuids)")
        else:
            print(f"{FAIL}  unintended duplicates: {bad_dupes} (main x{main_count})")
            failures += 1

        return 1 if failures else 0
    finally:
        runner_agy.resolve_cli_binary = original_resolve  # type: ignore
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(_AGY_HOME, ignore_errors=True)
        shutil.rmtree(stub_path.parent, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
