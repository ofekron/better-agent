#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
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

ignored_path = home / "indexes" / "ignored-sidecar.sqlite3"
ignored_wal = RootChangeWal(ignored_path)
ignored_wal.open()
ignored_wal.append_many((
    (
        "upsert",
        "attention_markers",
        sessions / "attention_markers.json",
        (1, 2, 3, 4, 5),
    ),
))
ignored_wal.close()
ignored_attempts: list[str] = []

def ignore_attention_markers(change: RootChange) -> None:
    ignored_attempts.append(change.root_id)
    if change.path.name != "attention_markers.json":
        raise RuntimeError("unexpected projection")

ignored_owner = RootChangeOwner(
    wal=RootChangeWal(ignored_path),
    roots=lambda: (),
    apply=ignore_attention_markers,
    poll_interval_s=60,
)
ignored_owner.start()
ignored_owner.wait_ready(3)
ignored_owner.stop()
inspection = RootChangeWal(ignored_path)
inspection.open()
assert ignored_attempts == ["attention_markers", "attention_markers"]
assert inspection.checkpoint("session-root-projection") == 2
inspection.close()

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
intent = local_owner.begin_local_upsert("local", local_root)
pending = local_owner.durable_local(intent, local_owner._signature(local_root))
local_owner.abandon_local(intent)
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
own_intent = own_owner.begin_local_upsert("root-a", root_a)
own_change = own_owner.durable_local(own_intent, own_owner._signature(root_a))
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
while {change.kind for change in bounded_applied} != {"upsert", "delete"}:
    count = bounded_owner.poll_once()
    counts.append(count)
    assert count <= 3, counts
    if len(counts) > 20:
        raise AssertionError("bounded watcher did not complete its cycle")
assert {change.kind for change in bounded_applied} == {"upsert", "delete"}
assert bounded_wal.append_transactions == 2
assert bounded_wal.projection_transactions == 2
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

# A local intent published before durable I/O prevents a stale scan from
# treating the writer's eventual file state as an external mutation.
intent_dir = home / "intent-sessions"
intent_dir.mkdir()
intent_file = intent_dir / "intent.json"
intent_file.write_text('{"v":1}')
intent_applied: list[RootChange] = []
intent_owner = RootChangeOwner(
    wal=RootChangeWal(home / "indexes" / "intent.sqlite3"),
    roots=lambda: (intent_dir,), apply=intent_applied.append,
    max_entries_per_tick=1, poll_interval_s=60,
)
intent_owner.start()
intent_owner.wait_ready(3)
intent_applied.clear()
mutation = intent_owner.begin_local_upsert("intent", intent_file)
intent_file.write_text('{"v":2}')
assert intent_owner.poll_once() == 1
change = intent_owner.durable_local(mutation, intent_owner._signature(intent_file))
intent_owner.complete_local(change)
intent_owner.poll_once()
assert intent_applied == []
intent_owner.stop()

# Deletes require absence in two complete authority cycles; a transient missing
# entry cannot remove a live projection.
verified_dir = home / "verified-delete-sessions"
verified_dir.mkdir()
verified_file = verified_dir / "verified.json"
verified_file.write_text("{}")
verified_applied: list[RootChange] = []
verified_owner = RootChangeOwner(
    wal=RootChangeWal(home / "indexes" / "verified.sqlite3"),
    roots=lambda: (verified_dir,), apply=verified_applied.append,
    max_entries_per_tick=100, poll_interval_s=60,
)
verified_owner.start()
verified_owner.wait_ready(3)
verified_applied.clear()
verified_file.unlink()
verified_owner.poll_once()
assert verified_applied == []
verified_file.write_text("{}")
verified_owner.poll_once()
assert [change.kind for change in verified_applied] == ["upsert"]
verified_applied.clear()
verified_file.unlink()
verified_owner.poll_once()
assert verified_applied == []
verified_owner.poll_once()
assert [change.kind for change in verified_applied] == ["delete"]
verified_owner.stop()

# Both budgets stop a cycle independently of the entry limit.
budget_dir = home / "budget-sessions"
budget_dir.mkdir()
for index in range(20):
    (budget_dir / f"budget-{index}.json").write_text("{}")
budget_owner = RootChangeOwner(
    wal=RootChangeWal(home / "indexes" / "budget.sqlite3"),
    roots=lambda: (budget_dir,), apply=lambda change: None,
    max_entries_per_tick=10_000, max_tick_wall_ms=0.001,
    max_tick_cpu_ms=1000, poll_interval_s=60,
)
budget_owner.start()
budget_owner.wait_ready(3)
assert budget_owner.poll_once() < 20
budget_owner.stop()


class ScheduledOwner(RootChangeOwner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.poll_count = 0
        self.poll_times: list[float] = []
        self.poll_entered = threading.Event()
        self.poll_release: threading.Event | None = None

    def poll_once(self):
        self.poll_count += 1
        self.poll_times.append(time.monotonic())
        self.poll_entered.set()
        if self.poll_release is not None:
            self.poll_release.wait(1)
        return super().poll_once()


# Idle authority checks back off instead of waking four times per second.
idle_dir = home / "idle-sessions"
idle_dir.mkdir()
idle_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "idle.sqlite3"),
    roots=lambda: (idle_dir,), apply=lambda change: None,
    poll_interval_s=0.02, max_poll_interval_s=0.08,
)
idle_owner.start()
idle_owner.wait_ready(3)
time.sleep(0.19)
assert idle_owner.poll_count <= 3, idle_owner.poll_count
idle_owner.stop()

# Continuous local intents cannot postpone the already-due authority pass.
churn_dir = home / "churn-sessions"
churn_dir.mkdir()
churn_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "churn.sqlite3"),
    roots=lambda: (churn_dir,), apply=lambda change: None,
    poll_interval_s=0.02, max_poll_interval_s=0.08,
)
churn_owner.start()
churn_owner.wait_ready(3)
time.sleep(0.07)
baseline_polls = churn_owner.poll_count
first_mutation_at = time.monotonic()
churn_deadline = first_mutation_at + 0.07
while time.monotonic() < churn_deadline:
    mutation = churn_owner.begin_local_delete("transient", churn_dir / "transient.json")
    churn_owner.abandon_local(mutation)
    time.sleep(0.005)
assert churn_owner.poll_count > baseline_polls, churn_owner.poll_count
assert churn_owner.poll_times[baseline_polls] - first_mutation_at <= 0.04
churn_owner.stop()

# Concurrent observation waiters coalesce onto one demanded authority cycle.
coalesce_dir = home / "coalesce-sessions"
coalesce_dir.mkdir()
coalesce_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "coalesce.sqlite3"),
    roots=lambda: (coalesce_dir,), apply=lambda change: None,
    poll_interval_s=60, max_poll_interval_s=60,
)
coalesce_owner.poll_release = threading.Event()
coalesce_owner.start()
coalesce_owner.wait_ready(3)
generation = coalesce_owner.observation_generation
barrier = threading.Barrier(9)
results: list[bool] = []

def wait_for_shared_observation() -> None:
    barrier.wait()
    results.append(coalesce_owner.wait_for_observation(generation, 1))

waiters = [threading.Thread(target=wait_for_shared_observation) for _ in range(8)]
for waiter in waiters:
    waiter.start()
barrier.wait()
assert coalesce_owner.poll_entered.wait(1)
late_result: list[bool] = []
late_waiter = threading.Thread(
    target=lambda: late_result.append(
        coalesce_owner.wait_for_observation(generation, 1)
    )
)
late_waiter.start()
time.sleep(0.01)
coalesce_owner.poll_release.set()
for waiter in waiters:
    waiter.join(1)
late_waiter.join(1)
assert results == [True] * 8, results
assert late_result == [True], late_result
assert coalesce_owner.poll_count == 1, coalesce_owner.poll_count
coalesce_owner.stop()

# External writes remain authority-bounded at the configured idle cap.
external_dir = home / "external-sessions"
external_dir.mkdir()
external_applied: list[RootChange] = []
external_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "external.sqlite3"),
    roots=lambda: (external_dir,), apply=external_applied.append,
    poll_interval_s=0.02, max_poll_interval_s=0.06,
)
external_owner.start()
external_owner.wait_ready(3)
time.sleep(0.07)
external_applied.clear()
(external_dir / "outside.json").write_text("{}")
deadline = time.monotonic() + 0.09
while not external_applied and time.monotonic() < deadline:
    time.sleep(0.002)
assert [change.root_id for change in external_applied] == ["outside"]
external_owner.stop()

# Deletes remain bounded by two authority cycles because absence is verified.
delete_bound_dir = home / "delete-bound-sessions"
delete_bound_dir.mkdir()
delete_bound_file = delete_bound_dir / "outside-delete.json"
delete_bound_file.write_text("{}")
delete_bound_applied: list[RootChange] = []
delete_bound_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "delete-bound.sqlite3"),
    roots=lambda: (delete_bound_dir,), apply=delete_bound_applied.append,
    poll_interval_s=0.015, max_poll_interval_s=0.03,
)
delete_bound_owner.start()
delete_bound_owner.wait_ready(3)
delete_bound_applied.clear()
delete_bound_file.unlink()
deadline = time.monotonic() + 0.075
while not delete_bound_applied and time.monotonic() < deadline:
    time.sleep(0.002)
assert [change.kind for change in delete_bound_applied] == ["delete"]
delete_bound_owner.stop()

# A retained failed snapshot retries with a bound instead of hot-spinning, and
# a later observation demand is serviced after the projection recovers.
failure_retry_dir = home / "failure-retry-sessions"
failure_retry_dir.mkdir()
failure_retry_file = failure_retry_dir / "retry.json"
failure_retry_file.write_text("{}")
failure_retry_enabled = True
failure_retry_applied: list[RootChange] = []

def recoverable_apply(change: RootChange) -> None:
    if failure_retry_enabled:
        raise RuntimeError("injected transient projection failure")
    failure_retry_applied.append(change)

failure_retry_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "failure-retry.sqlite3"),
    roots=lambda: (failure_retry_dir,), apply=recoverable_apply,
    poll_interval_s=0.2, max_poll_interval_s=0.4,
)
# Bootstrap must succeed; inject failure only after readiness.
failure_retry_enabled = False
failure_retry_owner.start()
failure_retry_owner.wait_ready(3)
failure_retry_applied.clear()
failure_retry_enabled = True
failure_retry_file.write_text('{"changed":true}')
generation = failure_retry_owner.observation_generation
assert not failure_retry_owner.wait_for_observation(generation, 0.02)
polls_after_failure = failure_retry_owner.poll_count
time.sleep(0.05)
assert failure_retry_owner.poll_count == polls_after_failure
failure_retry_enabled = False
generation = failure_retry_owner.observation_generation
assert failure_retry_owner.wait_for_observation(generation, 1)
assert [change.root_id for change in failure_retry_applied] == ["retry"]
failure_retry_owner.stop()

# One observation demand drives every bounded tick until its cycle completes.
continuation_dir = home / "continuation-sessions"
continuation_dir.mkdir()
for index in range(12):
    (continuation_dir / f"entry-{index}.json").write_text("{}")
continuation_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "continuation.sqlite3"),
    roots=lambda: (continuation_dir,), apply=lambda change: None,
    max_entries_per_tick=2, poll_interval_s=60, max_poll_interval_s=60,
)
continuation_owner.start()
continuation_owner.wait_ready(3)
generation = continuation_owner.observation_generation
assert continuation_owner.wait_for_observation(generation, 1)
assert continuation_owner.poll_count >= 6, continuation_owner.poll_count
continuation_owner.stop()

# Stop notifies the scheduler instead of waiting for a distant authority deadline.
stop_owner = ScheduledOwner(
    wal=RootChangeWal(home / "indexes" / "stop.sqlite3"),
    roots=lambda: (), apply=lambda change: None,
    poll_interval_s=60, max_poll_interval_s=60,
)
stop_owner.start()
stop_owner.wait_ready(3)
started = time.monotonic()
stop_owner.stop()
assert time.monotonic() - started < 0.2

print("PASS: durable root owner ordering, bootstrap diff, batching, failure, and crash replay")
