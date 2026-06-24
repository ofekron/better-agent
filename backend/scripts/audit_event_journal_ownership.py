"""Replay existing journals through the new ownership resolver without mutation.

Run with:
    cd backend
    .venv/bin/python scripts/audit_event_journal_ownership.py SESSION_ID [...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SOURCE_HOME = Path(
    os.environ.get("BETTER_CLAUDE_HOME") or Path.home() / ".better-claude",
).expanduser()
import _test_home
_TMP_HOME = _test_home.isolate("bc-audit-event-ownership-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from event_journal import (  # noqa: E402
    RENDER_EVENT_TYPES,
    Event,
    EventJournalReader,
    EventJournalWriter,
)


def _read_source_rows(session_id: str) -> list[dict]:
    path = _SOURCE_HOME / "sessions" / session_id / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _uuid_ownership(rows: list[dict]) -> dict[str, str | None]:
    return {
        str(data["uuid"]): row.get("msg_id")
        for row in rows
        if row.get("type") in RENDER_EVENT_TYPES
        and isinstance((data := row.get("data")), dict)
        and isinstance(data.get("uuid"), str)
        and data["uuid"]
    }


def _replay(session_id: str, rows: list[dict]) -> list[dict]:
    source_snapshot = _SOURCE_HOME / "sessions" / f"{session_id}.json"
    if source_snapshot.exists():
        replay_sessions = Path(_TMP_HOME) / "sessions"
        replay_sessions.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_snapshot, replay_sessions / source_snapshot.name)
    writer = EventJournalWriter()
    try:
        for row in rows:
            event_type = str(row.get("type") or "unknown")
            if event_type == "event_ownership_resolved":
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            writer.submit_event_sync(Event(
                root_id=session_id,
                sid=str(row.get("sid") or session_id),
                event_type=event_type,
                data=data,
                source=str(row.get("source") or "ownership-audit"),
                run_id=row.get("run_id"),
                event_id=f"audit-{row.get('seq')}",
                turn_id=data.get("turn_id"),
                message_id=row.get("msg_id"),
            ))
    finally:
        writer.close()
        event_ingester.close_all()
    replayed, _, _ = EventJournalReader().read_events(
        session_id, limit=999_999,
    )
    return replayed


def _audit(session_id: str, sample_limit: int) -> bool:
    old_rows = _read_source_rows(session_id)
    new_rows = _replay(session_id, old_rows)
    old = _uuid_ownership(old_rows)
    new = _uuid_ownership(new_rows)
    deltas = [
        (event_id, old[event_id], new.get(event_id))
        for event_id in old
        if old[event_id] != new.get(event_id)
    ]
    newly_owned = [delta for delta in deltas if delta[1] is None and delta[2]]
    changed_owner = [delta for delta in deltas if delta[1] and delta[2]]
    lost_owner = [delta for delta in deltas if delta[1] and not delta[2]]
    unresolved = sum(owner is None for owner in new.values())

    print(
        f"{session_id}: render_uuid_events={len(old)} deltas={len(deltas)} "
        f"newly_owned={len(newly_owned)} changed_owner={len(changed_owner)} "
        f"lost_owner={len(lost_owner)} unresolved_after={unresolved}",
    )
    for event_id, before, after in deltas[:sample_limit]:
        print(f"  {event_id}: {before!r} -> {after!r}")
    return not changed_owner and not lost_owner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_ids", nargs="+")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()
    try:
        safe = True
        for sid in args.session_ids:
            safe = _audit(sid, args.sample_limit) and safe
        return 0 if safe else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
