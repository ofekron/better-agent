"""Ban runtime event journal reads outside EventJournalReader.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_reader_boundary.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ALLOWED = {
    "event_ingester.py",
    "event_journal.py",
}
DIRECT_READ = re.compile(
    r"event_ingester\.(?:"
    r"read_events|read_orphan_events|read_ws_events|read_ws_events_range|"
    r"message_event_summaries|current_seq|cursor|max_seq_by_sid"
    r")\s*\(",
)
DIRECT_WRITE = re.compile(r"event_ingester\.ingest\s*\(")


def main() -> int:
    violations: list[str] = []
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND)
        if relative.parts[0] == "scripts":
            continue
        if any(part in {".venv", "venv", "__pycache__"} for part in relative.parts):
            continue
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            if DIRECT_READ.search(line):
                violations.append(f"{relative}:{line_number}: {line.strip()}")
            if DIRECT_WRITE.search(line):
                violations.append(f"{relative}:{line_number}: {line.strip()}")
    if violations:
        print("FAIL: runtime journal access bypasses EventJournalReader/Writer")
        print("\n".join(violations))
        return 1
    print("PASS: runtime journal access is owned by EventJournalReader/Writer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
