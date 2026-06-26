"""Regression test for agy cross-app-session conversation contamination.

Locks the fix for the bug where one Better Agent app session displayed
another app session's turn. The agy/antigravity runner discovered the agy
conversation id for a fresh turn via a cwd-keyed fallback
(`last_conversations.json[cwd]`). Because many app sessions share a single
cwd (e.g. the repo root), that fallback returned a DIFFERENT app session's
conversation; `_watch_conversation` latched it and `_watch_stream` then
tailed the foreign conversation's SQLite db, streaming its content into the
wrong session.

The fix removes the cwd fallback entirely: a run's conversation id may come
ONLY from this run's own agy CLI log markers or the app session's own
resumed id. Until one is available, discovery returns None (fail closed),
so `_watch_stream` has no sid and streams nothing — never a foreign one.

Run with:
    cd backend && .venv/bin/python scripts/test_runner_agy_session_isolation.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing any backend module.
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-agy-isolation-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runner_agy  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

# A conversation that belongs to a DIFFERENT app session but happens to share
# this run's cwd. The pre-fix code returned this from the cwd cache.
_FOREIGN_SID = "ffffffff-0000-0000-0000-000000000001"
# This run's OWN conversation, as it appears in agy's own CLI log markers.
_OWN_SID = "00000000-0000-0000-0000-0000000000aa"
_SHARED_CWD = "/Users/ofekron/better-claude"


def _make_conversation_db(home: Path, sid: str, text: bytes) -> None:
    """Create an agy conversation db with one renderable step, so
    _conversation_exists() is True and _stream_new_events() could tail it."""
    db_path = runner_agy._conversation_db(home, sid)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "create table if not exists steps (idx integer, step_type integer, status integer, "
            "has_subtrajectory integer, metadata blob, step_payload blob, render_info blob)"
        )
        con.execute("insert into steps values (0, 15, 0, 0, ?, ?, ?)", (b"", text, b""))
        con.commit()
    finally:
        con.close()


def _write_cwd_cache(home: Path, mapping: dict[str, str]) -> None:
    """Reproduce agy CLI's cwd-keyed last_conversations cache — the source the
    pre-fix fallback consulted."""
    import json
    cache = home / ".gemini" / "antigravity-cli" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "last_conversations.json").write_text(json.dumps(mapping), encoding="utf-8")


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="bc-test-agy-isolation-home-"))
    run_dir = Path(tempfile.mkdtemp(prefix="bc-test-agy-isolation-run-"))
    failures = 0
    try:
        # A foreign session's conversation exists on disk AND is the cwd cache's
        # current entry — exactly the contamination setup.
        _make_conversation_db(home, _FOREIGN_SID, b"foreign session secret content")
        _write_cwd_cache(home, {_SHARED_CWD: _FOREIGN_SID})

        empty_log = run_dir / "agy_cli.log"  # not yet written by agy CLI

        # (1) Core regression: a fresh turn whose own marker hasn't landed yet
        #     must NOT adopt the cwd-shared foreign conversation. Pre-fix this
        #     returned _FOREIGN_SID; post-fix it returns None (keep polling).
        sid = runner_agy._discover_conversation_id(
            empty_log, preferred=None, agy_home=home,
        )
        if sid is None:
            print(f"{PASS}  fresh turn with no marker yet discovers None (no cwd fallback)")
        else:
            print(f"{FAIL}  discovery leaked a foreign/cwd conversation: {sid!r}")
            failures += 1

        # (2) Streaming isolation (the user-visible symptom): because discovery
        #     yields None, _watch_stream never gets a sid, so it never tails the
        #     foreign db. Assert the foreign content is reachable IF a sid were
        #     set, proving the gate (not absence of data) is what protects us.
        emitted: dict[str, object] = {"count": 0, "seen": set()}
        events_path = run_dir / "session_events.jsonl"
        if sid is None:
            # No sid -> no streaming call happens; events file stays empty.
            if not events_path.exists():
                # Sanity: confirm the foreign db DOES carry streamable content,
                # so the None gate is the only thing preventing the leak.
                runner_agy._stream_new_events(
                    events_path, agy_home=home, conversation_id=_FOREIGN_SID,
                    parent_uuid=_FOREIGN_SID, emitted=emitted,
                )
                leaked = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
                if "foreign session secret content" in leaked:
                    print(f"{PASS}  foreign db is streamable; only the None-sid gate blocks it")
                else:
                    print(f"{FAIL}  foreign db produced no streamable content — test is vacuous")
                    failures += 1
            else:
                print(f"{FAIL}  events file unexpectedly present before any stream call")
                failures += 1

        # (3) Own marker present -> discovery returns OWN id (authoritative).
        log_with_marker = run_dir / "agy_cli_own.log"
        log_with_marker.write_text(
            f"I0625 09:00:00.0 1 server.go:800] Created conversation {_OWN_SID}\n",
            encoding="utf-8",
        )
        sid_own = runner_agy._discover_conversation_id(
            log_with_marker, preferred=None, agy_home=home,
        )
        if sid_own == _OWN_SID:
            print(f"{PASS}  own CLI log marker yields this run's conversation id")
        else:
            print(f"{FAIL}  expected {_OWN_SID}, got {sid_own!r}")
            failures += 1

        # (3b) Marker-beats-preferred precedence: if the run's own CLI log shows
        #      a conversation id that differs from the resumed `preferred` id
        #      (e.g. agy ignored --conversation and created a new one), the
        #      actual run's marker is authoritative — never the stale preferred.
        marker_sid = "00000000-0000-0000-0000-0000000000bb"
        log_diverged = run_dir / "agy_cli_diverged.log"
        log_diverged.write_text(
            f"I0625 09:00:00.0 1 server.go:800] Created conversation {marker_sid}\n",
            encoding="utf-8",
        )
        _make_conversation_db(home, _OWN_SID, b"preferred (stale) content")
        sid_div = runner_agy._discover_conversation_id(
            log_diverged, preferred=_OWN_SID, agy_home=home,
        )
        if sid_div == marker_sid:
            print(f"{PASS}  run's own marker overrides a divergent preferred id")
        else:
            print(f"{FAIL}  expected marker {marker_sid}, got {sid_div!r}")
            failures += 1

        # (4) Resume of a fresh app session (no requested id) starts fresh — it
        #     must NOT inherit the cwd-shared foreign conversation. Pre-fix
        #     returned _FOREIGN_SID; post-fix returns "".
        resumed = runner_agy._resolve_resume_conversation(home, "")
        if resumed == "":
            print(f"{PASS}  empty requested id resolves to fresh conversation (no cwd fallback)")
        else:
            print(f"{FAIL}  empty requested id leaked a conversation: {resumed!r}")
            failures += 1

        # (5) Legit same-session resume still works: the app session's OWN id
        #     (exists on disk) is honored.
        _make_conversation_db(home, _OWN_SID, b"own session content")
        resumed_own = runner_agy._resolve_resume_conversation(home, _OWN_SID)
        if resumed_own == _OWN_SID:
            print(f"{PASS}  own existing conversation id is honored on resume")
        else:
            print(f"{FAIL}  own resume id not honored: {resumed_own!r}")
            failures += 1
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nAll agy session-isolation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
