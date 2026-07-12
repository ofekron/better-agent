#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-root-change-wal-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from root_change_wal import RootChange, RootChangeOwner, RootChangeWal


class CountingWal(RootChangeWal):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.append_transactions = 0
        self.projection_transactions = 0

    def append_many(self, changes):
        rows = super().append_many(changes)
        self.append_transactions += bool(rows)
        return rows

    def commit_projection(self, consumer, changes):
        super().commit_projection(consumer, changes)
        self.projection_transactions += bool(changes)


home = Path(os.environ["BETTER_AGENT_HOME"])
sessions = home / "sessions"
sessions.mkdir(parents=True)


def owner(path: Path, applied, *, wal_type=RootChangeWal) -> RootChangeOwner:
    return RootChangeOwner(
        wal=wal_type(path), roots=lambda: (sessions,), apply=applied.append,
        max_entries_per_tick=10_000, poll_interval_s=60,
    )


# Bootstrap is a durable diff. Its second pass fences a mutation racing pass one.
root_a = sessions / "root-a.json"
root_a.write_text('{"v":1}')
bootstrap_applied: list[RootChange] = []
wal_path = home / "indexes" / "root-changes.sqlite3"
first_owner = owner(wal_path, bootstrap_applied)
first_owner.start()
first_owner.wait_ready(3)
assert [(c.kind, c.root_id) for c in bootstrap_applied] == [("upsert", "root-a")]
first_owner.stop()

# Persisted owner signatures prevent unchanged bootstrap replay.
unchanged: list[RootChange] = []
second_owner = owner(wal_path, unchanged)
second_owner.start()
second_owner.wait_ready(3)
assert unchanged == []
second_owner.stop()

# Same-size offline replacement with restored mtime is detected by inode/ctime.
old_stat = root_a.stat()
replacement = sessions / "replacement.tmp"
replacement.write_text('{"v":2}')
os.replace(replacement, root_a)
os.utime(root_a, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
downtime_applied: list[RootChange] = []
third_owner = owner(wal_path, downtime_applied)
third_owner.start()
third_owner.wait_ready(3)
assert [(c.kind, c.root_id) for c in downtime_applied] == [("upsert", "root-a")]
third_owner.stop()

root_a.unlink()
deleted_offline: list[RootChange] = []
delete_owner = owner(wal_path, deleted_offline)
delete_owner.start()
delete_owner.wait_ready(3)
assert [(c.kind, c.root_id) for c in deleted_offline] == [("delete", "root-a")]
delete_owner.stop()
root_a.write_text('{"v":3}')

# One external batch uses one append transaction and one projection/checkpoint transaction.
for index in range(20):
    (sessions / f"batch-{index}.json").write_text("{}")
counting_wal = CountingWal(home / "indexes" / "counting.sqlite3")
batched: list[RootChange] = []
batch_owner = RootChangeOwner(
    wal=counting_wal, roots=lambda: (sessions,), apply=batched.append,
    max_entries_per_tick=10_000, poll_interval_s=60,
)
batch_owner.start()
batch_owner.wait_ready(3)
assert counting_wal.append_transactions == 1, counting_wal.append_transactions
assert counting_wal.projection_transactions == 1, counting_wal.projection_transactions
batch_owner.stop()

# A failed group applies at least once but advances neither checkpoint nor signatures.
failure_path = home / "indexes" / "failure.sqlite3"
failure_wal = RootChangeWal(failure_path)
failure_wal.open()
failure_wal.append_many((
    ("upsert", "fail-a", sessions / "fail-a.json", (1, 2, 3, 4, 5)),
    ("upsert", "fail-b", sessions / "fail-b.json", (6, 7, 8, 9, 10)),
))
failure_wal.close()
attempts: list[str] = []

def fail_second(change: RootChange) -> None:
    attempts.append(change.root_id)
    if change.root_id == "fail-b":
        raise RuntimeError("injected projection crash")

failed = RootChangeOwner(
    wal=RootChangeWal(failure_path), roots=lambda: (), apply=fail_second, poll_interval_s=60,
)
failed.start()
try:
    failed.wait_ready(3)
except RuntimeError:
    pass
else:
    raise AssertionError("projection failure was not propagated through readiness")
failed.stop()
inspection = RootChangeWal(failure_path)
inspection.open()
assert inspection.checkpoint("session-root-projection") == 0
assert inspection.owner_signatures("session-root-projection") == {}
inspection.close()
recovered: list[RootChange] = []
recovery = RootChangeOwner(
    wal=RootChangeWal(failure_path), roots=lambda: (), apply=recovered.append, poll_interval_s=60,
)
recovery.start()
recovery.wait_ready(3)
assert [c.root_id for c in recovered[:2]] == ["fail-a", "fail-b"], recovered
recovery.stop()

# Crash after durable local WAL but before projection/checkpoint replays on restart.
local_path = home / "indexes" / "local-crash.sqlite3"
local_root = sessions / "local.json"
local_root.write_text("{}")
local_first: list[RootChange] = []
local_owner = owner(local_path, local_first)
local_owner.start()
local_owner.wait_ready(3)
local_first.clear()
local_root.write_text('{"changed":true}')
pending = local_owner.begin_local_upsert("local", local_root)
local_owner.abandon_local()
local_owner.stop()
local_replayed: list[RootChange] = []
local_recovery = owner(local_path, local_replayed)
local_recovery.start()
local_recovery.wait_ready(3)
assert [c.seq for c in local_replayed] == [pending.seq]
local_recovery.stop()

# A completed own write updates known only after checkpoint and is not watcher-applied again.
own_path = home / "indexes" / "own.sqlite3"
own_applied: list[RootChange] = []
own_owner = owner(own_path, own_applied)
own_owner.start()
own_owner.wait_ready(3)
own_applied.clear()
root_a.write_text('{"own":true}')
own_change = own_owner.begin_local_upsert("root-a", root_a)
own_owner.complete_local(own_change)
own_owner.poll_once()
assert own_applied == []
own_owner.stop()

# Steady scans retain iterators, enforce the hard entry bound, and reconcile
# upserts/deletes only when the complete cycle is known.
bounded_dir = home / "bounded-sessions"
bounded_dir.mkdir()
for index in range(8):
    (bounded_dir / f"seed-{index}.json").write_text("{}")
bounded_applied: list[RootChange] = []
bounded_wal = CountingWal(home / "indexes" / "bounded.sqlite3")
bounded_owner = RootChangeOwner(
    wal=bounded_wal, roots=lambda: (bounded_dir,), apply=bounded_applied.append,
    max_entries_per_tick=3, poll_interval_s=60,
)
bounded_owner.start()
bounded_owner.wait_ready(3)
bounded_applied.clear()
bounded_wal.append_transactions = 0
bounded_wal.projection_transactions = 0
(bounded_dir / "new.json").write_text("{}")
(bounded_dir / "seed-0.json").unlink()
counts: list[int] = []
while not bounded_applied:
    count = bounded_owner.poll_once()
    counts.append(count)
    assert count <= 3, counts
    if len(counts) > 10:
        raise AssertionError("bounded watcher did not complete its cycle")
assert {change.kind for change in bounded_applied} == {"upsert", "delete"}
assert bounded_wal.append_transactions == 1
assert bounded_wal.projection_transactions == 1
bounded_applied.clear()
bounded_wal.append_transactions = 0
bounded_wal.projection_transactions = 0
assert bounded_owner.poll_once() <= 3
assert bounded_wal.append_transactions == 0
assert bounded_wal.projection_transactions == 0
bounded_owner.stop()

# A malformed/unreadable projection rejects the complete batch, advances no
# checkpoint/signatures, and retries the unchanged retained snapshot.
retry_dir = home / "retry-sessions"
retry_dir.mkdir()
retry_fail = False
retry_attempts: list[str] = []

def retry_apply(change: RootChange) -> bool:
    retry_attempts.append(change.root_id)
    return not retry_fail

retry_wal_path = home / "indexes" / "retry.sqlite3"
retry_owner = RootChangeOwner(
    wal=RootChangeWal(retry_wal_path), roots=lambda: (retry_dir,), apply=retry_apply,
    max_entries_per_tick=1, poll_interval_s=60,
)
retry_owner.start()
retry_owner.wait_ready(3)
(retry_dir / "malformed.json").write_text("not-json")
retry_fail = True
try:
    while True:
        retry_owner.poll_once()
except RuntimeError:
    pass
inspection = RootChangeWal(retry_wal_path)
inspection.open()
checkpoint_before_retry = inspection.checkpoint("session-root-projection")
assert inspection.owner_signatures("session-root-projection") == {}
inspection.close()
retry_fail = False
retry_owner.poll_once()
inspection = RootChangeWal(retry_wal_path)
inspection.open()
assert inspection.checkpoint("session-root-projection") > checkpoint_before_retry
assert list(inspection.owner_signatures("session-root-projection")) == [retry_dir / "malformed.json"]
inspection.close()
assert retry_attempts == ["malformed", "malformed"]
retry_owner.stop()

print("PASS: durable root owner ordering, bootstrap diff, batching, failure, and crash replay")
