"""Append exact seq-targeted ownership resolutions for an existing session.

Run only while no other backend process is writing the same journal:
    cd backend
    .venv/bin/python scripts/repair_event_journal_ownership.py SESSION_ID
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402
from event_journal import EventJournalReader, EventJournalWriter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    args = parser.parse_args()

    reader = EventJournalReader()
    before = len(reader.read_orphan_events(args.session_id))
    writer = EventJournalWriter()
    try:
        resolved = writer.reconcile_ownership_sync(args.session_id)
    finally:
        writer.close()
        event_ingester.close_all()
    after = len(EventJournalReader().read_orphan_events(args.session_id))
    print(
        f"{args.session_id}: resolved={resolved} "
        f"effective_orphans_before={before} effective_orphans_after={after}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
