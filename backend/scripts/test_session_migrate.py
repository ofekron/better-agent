"""Regression test for moved-session content migration.

Pins `session_migrate.migrate_session_content`:
  1. events.jsonl is copied with the source root sid rewritten to dst
     everywhere it appears — top-level `sid`, nested `data.sid`, AND
     inside path strings (the bug that motivated replacing the per-line
     JSON rewriter with full-UUID text substitution).
  2. event_summaries.json root_id/sid rewritten to dst.
  3. message_frontend_cache/ copied verbatim (content-addressed).
  4. Fork/worker sids (distinct UUIDs) are left untouched.
  5. native_paths is NOT carried (new session spawns a fresh provider sid).

Run with:
    cd backend && .venv/bin/python scripts/test_session_migrate.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import paths  # noqa: E402  (safe to import before engage; does not resolve home)

_TMP = tempfile.mkdtemp(prefix="ba_test_migrate_")
paths.engage_test_home(_TMP)

import session_migrate  # noqa: E402

SRC = "01b4b55f-00d4-42a6-96f2-2222241770d5"
DST = "55003396-be2d-42a3-9979-739c60a6b209"
FORK = "d78ef486-5494-4bd1-b3ed-0357123444b1"  # a worker fork sid; must survive


def _sessions_dir() -> Path:
    return paths.bc_home() / "sessions"


def _write(sid: str, name: str, text: str) -> None:
    d = _sessions_dir() / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _read(sid: str, name: str) -> str:
    return (_sessions_dir() / sid / name).read_text(encoding="utf-8")


def _setup_source() -> None:
    events = [
        {
            "seq": 1,
            "sid": SRC,
            "type": "command_received",
            "data": {
                "method": "POST",
                # path embeds the sid as a substring — must be rewritten too
                "path": f"/api/sessions/{SRC}/opened",
                "sid": SRC,
            },
        },
        {
            "seq": 2,
            "sid": SRC,
            "type": "worker_event",
            "data": {"fork_sid": FORK, "parent_sid": SRC},
        },
    ]
    _write(SRC, "events.jsonl", "\n".join(json.dumps(e) for e in events) + "\n")
    _write(
        SRC,
        "event_summaries.json",
        json.dumps({
            "summary_version": 5,
            "summaries": {
                "m1": {"root_id": SRC, "sid": SRC, "seq_start": 1},
                "m2": {"root_id": SRC, "sid": FORK, "seq_start": 2},
            },
        }),
    )
    _write(SRC, "event_meta.json", json.dumps({
        "max_seq_by_sid": {SRC: 2, FORK: 1},
    }))
    cache_dir = _sessions_dir() / SRC / "message_frontend_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "abc123.json").write_text('{"rendered": true}', encoding="utf-8")
    # native_paths exists on source but must NOT be carried.
    _write(SRC, "native_paths", json.dumps([{"owning_session": SRC}]))


def main() -> int:
    failures: list[str] = []

    _setup_source()
    session_migrate.migrate_session_content(SRC, DST)

    # 1. events.jsonl: root sid rewritten everywhere, fork sid preserved.
    ev_text = _read(DST, "events.jsonl")
    if SRC in ev_text:
        failures.append("events.jsonl still contains source root sid")
    if DST not in ev_text:
        failures.append("events.jsonl missing dest root sid")
    if FORK not in ev_text:
        failures.append("events.jsonl dropped the fork sid (should be preserved)")
    if f"/api/sessions/{DST}/opened" not in ev_text:
        failures.append("events.jsonl path substring was not rewritten to dest sid")
    ev_lines = [json.loads(l) for l in ev_text.splitlines() if l.strip()]
    if ev_lines[0]["data"]["sid"] != DST:
        failures.append("nested data.sid was not rewritten")
    if ev_lines[1]["data"]["fork_sid"] != FORK:
        failures.append("fork sid inside data was corrupted")

    # 2. event_summaries.json: root_id/sid for root messages rewritten,
    #    fork-owned summary keeps fork sid but root_id becomes DST.
    summ = json.loads(_read(DST, "event_summaries.json"))
    if summ["summaries"]["m1"]["root_id"] != DST or summ["summaries"]["m1"]["sid"] != DST:
        failures.append("event_summaries root-owned sid not rewritten")
    if summ["summaries"]["m2"]["sid"] != FORK:
        failures.append("event_summaries fork-owned sid corrupted")
    if summ["summaries"]["m2"]["root_id"] != DST:
        failures.append("event_summaries fork-owned root_id not rewritten")

    # 3. message_frontend_cache copied verbatim.
    cache_file = _sessions_dir() / DST / "message_frontend_cache" / "abc123.json"
    if not cache_file.is_file() or cache_file.read_text() != '{"rendered": true}':
        failures.append("message_frontend_cache not copied verbatim")

    # 4. native_paths NOT carried.
    if (_sessions_dir() / DST / "native_paths").exists():
        failures.append("native_paths was carried (must not be)")

    # 5. event_meta rewritten.
    meta = json.loads(_read(DST, "event_meta.json"))
    if SRC in meta["max_seq_by_sid"] or DST not in meta["max_seq_by_sid"]:
        failures.append("event_meta root sid key not rewritten")

    # 6. Idempotent re-run reproduces identical dst events.jsonl.
    first = _read(DST, "events.jsonl")
    session_migrate.migrate_session_content(SRC, DST)
    if _read(DST, "events.jsonl") != first:
        failures.append("re-run is not idempotent")

    # 7. Validation: rejects equal/empty sids.
    try:
        session_migrate.migrate_session_content(SRC, SRC)
        failures.append("equal sids should raise ValueError")
    except ValueError:
        pass

    if failures:
        print("FAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("OK: migrate_session_content rewrites root sid everywhere, "
          "preserves fork sids, copies cache, omits native_paths; idempotent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
