"""Locks the summary-index build against the SELF-deadlock, and verifies
phase-3 persistence + cross-pass eng-pointer survival.

Regression for the deadlock where `_ensure_summary_index` held the
non-reentrant `_summary_index_lock` across `_migrate_and_persist` ->
`write_session_full` -> `_upsert_summary` (which re-acquires the SAME
lock on the same thread -> permanent hang). Fires for any dirty session
(e.g. `_schema_version == 7`) during the first index build.

Checks:
  1. `list_sessions()` over a v7 session completes (no self-deadlock).
  2. Phase-3 persisted the v7 -> v8 strip to disk (schema 8, empty events).
  3. Eng-pointers collected from both Pass-1 (summary-file) and Pass-2
     (full-file) children are applied to parents.

Run with:
    cd backend && .venv/bin/python scripts/test_summary_index_no_self_deadlock.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sidx-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import event_journal  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

CWD = "/tmp/test-sidx"


def _native_event(uuid: str, text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
    }


def _sessions_dir():
    d = ba_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_full(record: dict) -> None:
    with open(_sessions_dir() / f"{record['id']}.json", "w") as f:
        json.dump(record, f)


def _write_summary(sid: str, summary: dict) -> None:
    # Written AFTER the full file so its mtime is >= the full file's,
    # making Pass-1 load it (summary_mtime >= session_mtime).
    with open(_sessions_dir() / f"{sid}.summary.json", "w") as f:
        json.dump(summary, f)


def _v7_record(sid: str, uuids: list[str]) -> dict:
    return {
        "_schema_version": 7,
        "id": sid,
        "name": sid,
        "model": "sonnet",
        "cwd": CWD,
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [
            {"id": "msg-1", "role": "user", "content": "u", "events": [], "seq": 0},
            {
                "id": "msg-2",
                "role": "assistant",
                "content": "",
                "events": [_native_event(u, f"t-{u}") for u in uuids],
                "seq": 1,
            },
        ],
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "source": "cli",
    }


def _v8_record(sid: str, **extra) -> dict:
    rec = {
        "_schema_version": 8,
        "id": sid,
        "name": sid,
        "model": "sonnet",
        "cwd": CWD,
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [
            {"id": "m1", "role": "assistant", "content": "x", "events": [], "seq": 1},
        ],
        "next_seq": 2,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "source": "cli",
    }
    rec.update(extra)
    return rec


def _report(results: list[tuple[str, bool, str]]) -> bool:
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        print(f"  {PASS if ok else FAIL} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    # Fixture 1: a v7 session (dirty -> triggers a write under the build).
    dead_sid = "v7-deadlock"
    _write_full(_v7_record(dead_sid, [f"d-{i}" for i in range(5)]))

    # Fixture 2: cross-pass eng-pointer. Parent loaded via Pass-1 (fresh
    # .summary.json), child via Pass-2 (full file with working_mode).
    parent_sid = "eng-parent"
    child_sid = "eng-child"
    pass1_parent_sid = "eng-pass1-parent"
    pass1_child_sid = "eng-pass1-child"
    _write_full(_v8_record(parent_sid))
    _write_summary(parent_sid, {
        "id": parent_sid, "name": parent_sid, "cwd": CWD,
        "working_mode": None, "working_mode_meta": None,
        "pending_eng_session_id": None, "updated_at": "2026-01-01T00:00:00",
        "last_seen_event_uid": None, "fork_count": 0, "fork_ids": [],
    })
    _write_full(_v8_record(
        child_sid,
        working_mode="prompt_engineering",
        working_mode_meta={"parent_session_id": parent_sid},
    ))
    _write_full(_v8_record(pass1_parent_sid))
    _write_summary(pass1_parent_sid, {
        "id": pass1_parent_sid, "name": pass1_parent_sid, "cwd": CWD,
        "working_mode": None, "working_mode_meta": None,
        "pending_eng_session_id": None, "updated_at": "2026-01-01T00:00:00",
        "last_seen_event_uid": None, "fork_count": 0, "fork_ids": [],
    })
    _write_full(_v8_record(
        pass1_child_sid,
        working_mode="prompt_engineering",
        working_mode_meta={"parent_session_id": pass1_parent_sid},
    ))
    _write_summary(pass1_child_sid, {
        "id": pass1_child_sid, "name": pass1_child_sid, "cwd": CWD,
        "working_mode": "prompt_engineering",
        "working_mode_meta": {"parent_session_id": pass1_parent_sid},
        "pending_eng_session_id": None, "updated_at": "2026-01-01T00:00:00",
        "last_seen_event_uid": None, "fork_count": 0, "fork_ids": [],
    })

    # --- Check 1: the blocking first build must NOT self-deadlock. ---
    box: dict = {}

    original_publish = event_journal.publish_event_sync

    def call():
        event_journal.publish_event_sync = lambda **_: None
        try:
            session_store._ensure_summary_index(blocking=True)
            box["res"] = session_store.list_sessions()
        finally:
            event_journal.publish_event_sync = original_publish

    th = threading.Thread(target=call, daemon=True)
    th.start()
    th.join(timeout=10)
    completed = not th.is_alive()
    results.append((
        "_ensure_summary_index(blocking=True) completes (no self-deadlock)", completed,
        "thread still alive after 10s -> deadlock",
    ))
    if not completed:
        return _report(results)

    sessions = {s["id"]: s for s in box["res"]}

    # --- Check 2: phase-3 persisted the v7 -> v8 strip to disk. ---
    raw = json.loads(open(session_store._session_path(dead_sid)).read())
    results.append((
        f"phase-3: on-disk _schema_version == {session_store.SCHEMA_VERSION}",
        raw.get("_schema_version") == session_store.SCHEMA_VERSION,
        f"got {raw.get('_schema_version')}",
    ))
    results.append((
        "phase-3: on-disk msg.events stripped",
        all(("events" not in m) for m in raw.get("messages", [])),
        "some msg.events still present on disk",
    ))

    # --- Check 3: eng-pointer survives the cross-pass build. ---
    got = sessions.get(parent_sid, {}).get("pending_eng_session_id")
    results.append((
        "eng-pointer applied to Pass-1 parent from Pass-2 child",
        got == child_sid, f"got {got}",
    ))
    got_pass1 = sessions.get(pass1_parent_sid, {}).get("pending_eng_session_id")
    results.append((
        "eng-pointer applied to Pass-1 parent from Pass-1 child",
        got_pass1 == pass1_child_sid, f"got {got_pass1}",
    ))

    return _report(results)


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
