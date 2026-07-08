"""Regression test: resuming an agy conversation (interrupt + new turn) MUST
NOT re-emit prior turns' events into the new turn.

agy's conversation DB is cumulative (append-only by step idx), so prior turns'
steps are still present when a new turn resumes the same conversation. The
runner seeds the streaming dedup set with the prior turns' stabilized uuids
(``_prior_turn_uuids``, snapshotted before spawn) so only the new turn's steps
stream. Pre-fix, a fresh run_dir left ``seen`` empty and the whole cumulative
DB re-emitted -- the new turn filled with duplicates of the interrupted turn.

Run with:
    cd backend && .venv/bin/python scripts/test_agy_resume_no_duplication.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-agy-resume-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_AGY_HOME = Path(tempfile.mkdtemp(prefix="bc-test-agy-resume-home-"))

import runner_agy  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_SID = "aaaaaaaa-0000-0000-0000-000000000001"
_SEP = b"\x02"


def _view_file_step(idx: int, tool_id: str, path: str) -> tuple:
    payload = _SEP.join([
        tool_id.encode(), b"view_file",
        json.dumps({"AbsolutePath": path, "toolAction": f"Reading {path}"}).encode(),
        b"The file contents describe the project layout in detail for the reader.",
    ])
    return (idx, 8, b"", payload, b"")


def _build_db(steps: list[tuple]) -> Path:
    db_path = runner_agy._conversation_db(_AGY_HOME, _SID)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute(
        "create table steps (idx integer, step_type integer, status integer, "
        "has_subtrajectory integer, metadata blob, step_payload blob, render_info blob)"
    )
    con.executemany("insert into steps values (?, ?, 0, 0, ?, ?, ?)", steps)
    con.commit()
    con.close()
    return db_path


def _written_tool_ids(events_path: Path) -> set[str]:
    ids: set[str] = set()
    if not events_path.is_file():
        return ids
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        for b in ev.get("data", {}).get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                ids.add(b.get("id"))
    return ids


def main() -> int:
    failures = 0
    events_path = Path(tempfile.mkdtemp(prefix="bc-test-agy-resume-run-")) / "session_events.jsonl"
    try:
        # Prior turn already in the cumulative DB (step 0).
        _build_db([_view_file_step(0, "prior_tool", "/tmp/old.txt")])

        # Snapshot prior-turn uuids BEFORE the new turn appends anything.
        prior_seen = runner_agy._prior_turn_uuids(agy_home=_AGY_HOME, conversation_id=_SID)
        if not prior_seen:
            print(f"{FAIL}  _prior_turn_uuids returned nothing for a non-empty DB")
            failures += 1

        emitted = {"seen": set(prior_seen)}

        # Stream with only the prior step present -> nothing should emit.
        runner_agy._stream_new_events(
            events_path, agy_home=_AGY_HOME, conversation_id=_SID,
            parent_uuid=_SID, emitted=emitted, include_prose=False,
        )
        if events_path.is_file() and events_path.read_text(encoding="utf-8").strip():
            print(f"{FAIL}  prior turn re-emitted into a resumed run: "
                  f"{_written_tool_ids(events_path)}")
            failures += 1
        else:
            print(f"{PASS}  prior turn suppressed on resume (no re-emit)")

        # New turn appends a new step; only it should stream.
        con = sqlite3.connect(str(runner_agy._conversation_db(_AGY_HOME, _SID)))
        con.execute(
            "insert into steps values (?, ?, 0, 0, ?, ?, ?)",
            _view_file_step(1, "new_tool", "/tmp/new.txt"),
        )
        con.commit()
        con.close()

        runner_agy._stream_new_events(
            events_path, agy_home=_AGY_HOME, conversation_id=_SID,
            parent_uuid=_SID, emitted=emitted, include_prose=False,
        )
        written = _written_tool_ids(events_path)
        if written == {"new_tool"}:
            print(f"{PASS}  new turn streams only its own step ({written})")
        else:
            print(f"{FAIL}  new turn streamed {written} (expected only new_tool) "
                  f"-- prior turn duplicated")
            failures += 1
    finally:
        shutil.rmtree(_AGY_HOME, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        if events_path.parent.exists():
            shutil.rmtree(events_path.parent, ignore_errors=True)

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nAll resume-dedup checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
