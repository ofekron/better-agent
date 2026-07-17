"""Append exact seq-targeted ownership resolutions for an existing session.

    cd backend
    .venv/bin/python scripts/repair_event_journal_ownership.py SESSION_ID

Takes the same instance lock the runtime holds for the whole process
lifetime (`backend_instance_lock`) so this can never run concurrently
with a live backend against the same home — it either waits for the
running backend to exit or fails fast with a clear error.
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from backend_instance_lock import (  # noqa: E402
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)
from event_ingester import event_ingester  # noqa: E402
from event_journal import EventJournalReader, EventJournalWriter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    args = parser.parse_args()

    acquire_backend_instance_lock()
    try:
        reader = EventJournalReader()
        before = len(reader.read_orphan_events(args.session_id))
        writer = EventJournalWriter()
        try:
            resolved = writer.reconcile_ownership_sync(args.session_id)
        finally:
            writer.close()
            event_ingester.close_all()
        after = len(EventJournalReader().read_orphan_events(args.session_id))
    finally:
        release_backend_instance_lock()
    print(
        f"{args.session_id}: resolved={resolved} "
        f"effective_orphans_before={before} effective_orphans_after={after}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
