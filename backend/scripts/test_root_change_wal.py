#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-root-change-wal-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from root_change_wal import RootChangeOwner, RootChangeWal


home = Path(os.environ["BETTER_AGENT_HOME"])
sessions = home / "sessions"
sessions.mkdir(parents=True)
wal_path = home / "indexes" / "root-changes.sqlite3"

applied = []
wal = RootChangeWal(wal_path)
wal.open()
first = wal.append("upsert", "root-a", sessions / "root-a.json", (1, 2, 3))
second = wal.append("delete", "root-a", sessions / "root-a.json", None)
assert second == first + 1
wal.close()

owner = RootChangeOwner(
    wal=RootChangeWal(wal_path),
    roots=lambda: (sessions,),
    apply=applied.append,
    max_entries_per_tick=2,
    poll_interval_s=60,
)
owner.start()
owner.stop()
assert [(item.seq, item.kind) for item in applied] == [(first, "upsert"), (second, "delete")]

# The durable cursor prevents a restart from applying acknowledged facts twice.
replayed = []
owner = RootChangeOwner(
    wal=RootChangeWal(wal_path),
    roots=lambda: (sessions,),
    apply=replayed.append,
    max_entries_per_tick=2,
    poll_interval_s=60,
)
owner.start()
owner.stop()
assert replayed == []

# A failed projection callback leaves its row behind the checkpoint for restart.
failure_wal = RootChangeWal(home / "indexes" / "failure.sqlite3")
failure_wal.open()
failure_wal.append("upsert", "root-b", sessions / "root-b.json", (4, 5, 6))
failure_wal.close()
attempts = 0

def fail_once(change):
    global attempts
    attempts += 1
    raise RuntimeError("injected projection crash")

failed_owner = RootChangeOwner(
    wal=RootChangeWal(home / "indexes" / "failure.sqlite3"),
    roots=lambda: (sessions,),
    apply=fail_once,
    poll_interval_s=60,
)
try:
    failed_owner.start()
except RuntimeError:
    pass
else:
    raise AssertionError("projection crash was not surfaced")
assert attempts == 1

recovered = []
recovery_owner = RootChangeOwner(
    wal=RootChangeWal(home / "indexes" / "failure.sqlite3"),
    roots=lambda: (sessions,),
    apply=recovered.append,
    poll_interval_s=60,
)
recovery_owner.start()
recovery_owner.stop()
assert [item.root_id for item in recovered] == ["root-b"]

# Polling work is bounded and sidecars can be excluded by the injected predicate.
for index in range(10):
    (sessions / f"root-{index}.json").write_text("{}")
(sessions / "root-0.summary.json").write_text("{}")
observed = []
bounded = RootChangeOwner(
    wal=RootChangeWal(home / "indexes" / "bounded.sqlite3"),
    roots=lambda: (sessions,),
    apply=observed.append,
    accept_path=lambda path: path.name.endswith(".json") and ".summary." not in path.name,
    max_entries_per_tick=3,
    poll_interval_s=60,
)
bounded.start()
assert bounded.poll_once() <= 3
assert len(observed) <= 3
assert all("summary" not in item.path.name for item in observed)
bounded.stop()

print("PASS: root change WAL replay, crash cursor, typed facts, and bounded watcher")
