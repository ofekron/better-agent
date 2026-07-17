import json
import os
import resource
import signal
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="ba-hydration-index-")
os.environ["BETTER_AGENT_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hydration_index_store as store
from event_ingester import EventIngester, event_ingester


def _row(sid: str, value: int, newline: bool = True) -> bytes:
    data = json.dumps({"sid": sid, "seq": value}).encode()
    return data + (b"\n" if newline else b"")


def _next_digest(previous: str, row: bytes) -> str:
    import hashlib
    return hashlib.sha256(bytes.fromhex(previous) + row).hexdigest()


def main() -> int:
    for invalid_root in ("", ".", "..", "../escape", "nested/root", "nested\\root"):
        try:
            store._db_path(invalid_root)
            raise AssertionError(invalid_root)
        except ValueError:
            pass
    outside = Path(HOME) / "outside"
    outside.mkdir()
    symlink_root = Path(HOME) / "sessions" / "symlink-root"
    symlink_root.parent.mkdir(parents=True, exist_ok=True)
    symlink_root.symlink_to(outside, target_is_directory=True)
    try:
        store._db_path("symlink-root")
        raise AssertionError("symlink root accepted")
    except ValueError:
        pass
    symlink_journal = Path(HOME) / "sessions" / "journal-link" / "events.jsonl"
    symlink_journal.parent.mkdir(parents=True)
    symlink_target = outside / "events.jsonl"
    symlink_target.write_bytes(b"")
    symlink_journal.symlink_to(symlink_target)
    try:
        store.load("journal-link", symlink_journal)
        raise AssertionError("symlink journal accepted")
    except ValueError:
        pass

    journal = Path(HOME) / "sessions" / "root" / "events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(_row("a", 1))
    offsets, first = store.load("root", journal)
    assert first["cold"] == 1 and offsets["a"] == (0,), (offsets, first)

    original_size = journal.stat().st_size
    digest = _next_digest(bytes(32).hex(), _row("a", 1))
    appended_row = _row("b", 2)
    with journal.open("ab") as file:
        file.write(appended_row)
    next_digest = _next_digest(digest, appended_row)
    store.note_authoritative_append(
        "root", journal, original_size, journal.stat().st_size,
        digest, next_digest,
    )
    digest = next_digest
    offsets, appended = store.load("root", journal)
    assert appended["cold"] == 0, appended
    assert appended["scanned_bytes"] == journal.stat().st_size - original_size, appended
    assert offsets["b"] == (original_size,), offsets

    partial_start = journal.stat().st_size
    partial_row = _row("c", 3, newline=False)
    with journal.open("ab") as file:
        file.write(partial_row)
    store.note_authoritative_append(
        "root", journal, partial_start, journal.stat().st_size,
        digest, _next_digest(digest, partial_row),
    )
    offsets, _ = store.load("root", journal)
    assert "c" not in offsets
    with journal.open("ab") as file:
        file.write(b"\n")
    completed_row = partial_row + b"\n"
    completed_digest = _next_digest(digest, completed_row)
    store.note_authoritative_append(
        "root", journal, partial_start, journal.stat().st_size,
        digest, completed_digest,
    )
    digest = completed_digest
    offsets, completed = store.load("root", journal)
    assert offsets["c"] == (partial_start,), (offsets, completed)

    stable = journal.read_bytes()
    mutated = bytearray(stable)
    mutated[len(mutated) // 2] ^= 1
    journal.write_bytes(mutated)
    _, rewritten = store.load("root", journal)
    assert rewritten["cold"] == 1, rewritten
    journal.write_bytes(stable)
    _, restored = store.load("root", journal)
    assert restored["cold"] == 1, restored

    concurrent_start = journal.stat().st_size
    concurrent_row = _row("d", 4)
    with journal.open("ab") as file:
        file.write(concurrent_row)
    concurrent_digest = _next_digest(digest, concurrent_row)
    store.note_authoritative_append(
        "root", journal, concurrent_start, journal.stat().st_size,
        digest, concurrent_digest,
    )
    barrier = threading.Barrier(3)
    results = []

    def concurrent_load():
        barrier.wait()
        results.append(store.load("root", journal))

    threads = [threading.Thread(target=concurrent_load) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(results) == 2
    with sqlite3.connect(store._db_path("root")) as conn:
        assert conn.execute("SELECT count(*) FROM offsets WHERE sid='d'").fetchone()[0] == 1

    race_start = journal.stat().st_size
    race_first = _row("race-first", 40)
    with journal.open("ab") as file:
        file.write(race_first)
    race_first_digest = _next_digest(concurrent_digest, race_first)
    store.note_authoritative_append(
        "root", journal, race_start, journal.stat().st_size,
        concurrent_digest, race_first_digest,
    )
    original_scan = store._scan
    raced = False

    def append_during_scan(conn, scanned_journal, start, start_digest):
        nonlocal raced
        result = original_scan(conn, scanned_journal, start, start_digest)
        if not raced:
            raced = True
            second_start = scanned_journal.stat().st_size
            second = _row("race-second", 41)
            with scanned_journal.open("ab") as file:
                file.write(second)
            store.note_authoritative_append(
                "root", scanned_journal, second_start,
                scanned_journal.stat().st_size, result[3],
                _next_digest(result[3], second),
            )
        return result

    store._scan = append_during_scan
    try:
        _, raced_load = store.load("root", journal)
    finally:
        store._scan = original_scan
    assert raced_load["cold"] == 0, raced_load
    offsets, race_followup = store.load("root", journal)
    assert race_followup["cold"] == 0, race_followup
    assert offsets["race-second"], offsets
    concurrent_digest = _next_digest(race_first_digest, _row("race-second", 41))
    store.mark_reconciled("root", journal, 41)
    assert store.reconcile_cursor("root", journal) == 41

    post_cursor_start = journal.stat().st_size
    post_cursor_row = _row("post-cursor", 42)
    with journal.open("ab") as file:
        file.write(post_cursor_row)
    post_cursor_digest = _next_digest(concurrent_digest, post_cursor_row)
    store.note_authoritative_append(
        "root", journal, post_cursor_start, journal.stat().st_size,
        concurrent_digest, post_cursor_digest,
    )
    assert store.reconcile_cursor("root", journal) == 41
    concurrent_digest = post_cursor_digest

    rewritten_growth = bytearray(journal.read_bytes())
    rewritten_growth[len(rewritten_growth) // 3] ^= 1
    journal.write_bytes(rewritten_growth)
    growth_start = journal.stat().st_size
    rewrite_row = _row("e", 5)
    with journal.open("ab") as file:
        file.write(rewrite_row)
    store.note_authoritative_append(
        "root", journal, growth_start, journal.stat().st_size,
        concurrent_digest, _next_digest(concurrent_digest, rewrite_row),
    )
    _, rewrite_then_append = store.load("root", journal)
    assert rewrite_then_append["cold"] == 1, rewrite_then_append

    rewrite_root = "rewrite-root"
    for value in range(1, 501):
        event_ingester.ingest(
            rewrite_root, rewrite_root, "agent_message",
            {"uuid": f"rewrite-{value}", "padding": "x" * 64},
            source="hydration-index-test", msg_id="message",
        )
    rewrite_path = event_ingester._events_path(rewrite_root)
    _, rewrite_initial = store.load(rewrite_root, rewrite_path)
    assert rewrite_initial["cold"] == 0
    payload = bytearray(rewrite_path.read_bytes())
    mutation_offset = payload.index(b"x" * 16)
    assert mutation_offset < len(payload) - store.BOUNDARY_BYTES
    payload[mutation_offset] = ord("y")
    rewrite_path.write_bytes(payload)
    cached_handle = event_ingester._handles[rewrite_root][1]
    assert not event_ingester._chain_handle_current_locked(
        rewrite_root, rewrite_path, cached_handle,
    ), (
        event_ingester._chain_meta_identity.get(rewrite_root),
        event_ingester._chain_identity(rewrite_path.stat()),
    )
    event_ingester.ingest(
        rewrite_root, rewrite_root, "agent_message",
        {"uuid": "rewrite-after-mutation", "padding": "z" * 64},
        source="hydration-index-test", msg_id="message",
    )
    _, rewrite_rebuilt = store.load(rewrite_root, rewrite_path)
    assert rewrite_rebuilt["scanned_bytes"] == 0, rewrite_rebuilt
    event_ingester.close(rewrite_root)

    restart_root = "restart-root"
    for value in range(1, 101):
        event_ingester.ingest(
            restart_root, restart_root, "agent_message",
            {"uuid": f"restart-{value}"}, source="hydration-index-test",
            msg_id="message",
        )
    restart_path = event_ingester._events_path(restart_root)
    store.load(restart_root, restart_path)
    store.mark_reconciled(restart_root, restart_path, 100)
    event_ingester.ingest(
        restart_root, restart_root, "agent_message",
        {"uuid": "restart-101"}, source="hydration-index-test",
        msg_id="message",
    )
    event_ingester.close(restart_root)
    with store._receipts_lock:
        store._append_receipts.clear()
    _, restart_tail = store.load(restart_root, restart_path)
    assert restart_tail["cold"] == 0, restart_tail
    assert restart_tail["scanned_bytes"] == 0, restart_tail
    assert store._db_path(restart_root).parent == restart_path.parent.resolve()
    assert store.reconcile_cursor(restart_root, restart_path) == 100

    shared_root = "shared-receipt-root"
    shared_path = Path(HOME) / "sessions" / shared_root / "events.jsonl"
    shared_path.parent.mkdir(parents=True)
    shared_initial = _row(shared_root, 1)
    shared_path.write_bytes(shared_initial)
    store.load(shared_root, shared_path)
    shared_ack = json.loads(store._ack_path(shared_path).read_text())
    writer_code = r'''
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hydration_index_store as store
root, sid = sys.argv[2], sys.argv[3]
path = Path(sys.argv[4])
with store.journal_guard(root, path):
    try:
        prior = json.loads(store._receipt_path(path).read_text())
        digest = prior["digest"]
    except OSError:
        digest = json.loads(store._ack_path(path).read_text())["digest"]
    row = json.dumps({"sid": sid, "seq": int(sys.argv[5])}).encode() + b"\n"
    next_digest = hashlib.sha256(bytes.fromhex(digest) + row).hexdigest()
    with path.open("ab") as handle:
        handle.write(row)
        handle.flush()
        store.prepare_durable_append_receipt(root, path, handle.tell(), next_digest)
        os.fsync(handle.fileno())
'''
    for sid, seq in (("writer-a", 2), ("writer-b", 3), ("writer-a", 4), ("writer-b", 5)):
        subprocess.run([
            sys.executable, "-c", writer_code,
            str(Path(__file__).resolve().parents[1]), shared_root, sid,
            str(shared_path), str(seq),
        ], env={**os.environ, "BETTER_AGENT_HOME": HOME}, check=True)
    with store._receipts_lock:
        store._append_receipts.clear()
    shared_offsets, shared_tail = store.load(shared_root, shared_path)
    assert shared_tail["cold"] == 0, shared_tail
    assert 0 < shared_tail["scanned_bytes"] < shared_path.stat().st_size
    assert len(shared_offsets["writer-a"]) == 2
    assert len(shared_offsets["writer-b"]) == 2
    assert json.loads(store._ack_path(shared_path).read_text())["offset"] == shared_path.stat().st_size

    real_root = "concurrent-real-ingesters"
    real_ready = Path(HOME) / "real-ready"
    real_start = Path(HOME) / "real-start"
    real_writer = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from event_ingester import EventIngester
root, writer = sys.argv[2], sys.argv[3]
ready, start = Path(sys.argv[4]), Path(sys.argv[5])
ingester = EventIngester()
ready.mkdir(parents=True, exist_ok=True)
(ready / writer).touch()
while not start.exists():
    time.sleep(0.001)
for value in range(25):
    ingester.ingest(root, root, "agent_message", {"uuid": f"{writer}-{value}"}, source=writer, msg_id="message")
ingester.shutdown()
'''
    writers = [subprocess.Popen([
        sys.executable, "-c", real_writer,
        str(Path(__file__).resolve().parents[1]), real_root, writer,
        str(real_ready), str(real_start),
    ], env={**os.environ, "BETTER_AGENT_HOME": HOME}) for writer in ("a", "b")]
    deadline = time.monotonic() + 3
    while len(list(real_ready.glob("*"))) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(list(real_ready.glob("*"))) == 2
    real_start.touch()
    assert all(process.wait(timeout=15) == 0 for process in writers)
    real_path = Path(HOME) / "sessions" / real_root / "events.jsonl"
    rows = [json.loads(line) for line in real_path.read_text().splitlines()]
    assert len(rows) == 50
    assert len({row["data"]["uuid"] for row in rows}) == 50
    assert [row["seq"] for row in rows] == list(range(1, 51))
    real_offsets, real_metrics = store.load(real_root, real_path)
    assert real_metrics["scanned_bytes"] == 0, real_metrics
    assert sum(map(len, real_offsets.values())) == 50

    failure_root = "projection-failure-root"
    failure_path = Path(HOME) / "sessions" / failure_root / "events.jsonl"
    failure_path.parent.mkdir(parents=True)
    failure_first = _row(failure_root, 1)
    failure_path.write_bytes(failure_first)
    store.load(failure_root, failure_path)
    failure_ack = json.loads(store._ack_path(failure_path).read_text())
    failure_tail = _row(failure_root, 2)
    with store.journal_guard(failure_root, failure_path):
        with failure_path.open("ab") as handle:
            handle.write(failure_tail)
            handle.flush()
            failure_digest = _next_digest(failure_ack["digest"], failure_tail)
            store.prepare_durable_append_receipt(
                failure_root, failure_path, handle.tell(), failure_digest,
            )
            os.fsync(handle.fileno())
    original_scan = store._scan
    store._scan = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        sqlite3.OperationalError("injected projection failure")
    )
    try:
        try:
            store.flush_writer_projection(failure_root, failure_path)
            raise AssertionError("SQLite projection failure was swallowed")
        except store.WriterProjectionError:
            pass
    finally:
        store._scan = original_scan
    store.flush_writer_projection(failure_root, failure_path)
    _, failure_retried = store.load(failure_root, failure_path)
    assert failure_retried["scanned_bytes"] == 0, failure_retried

    retry_ingester = EventIngester()
    retry_root = "background-projection-retry"
    original_flush = store.flush_writer_projection
    retry_succeeded = threading.Event()
    retry_calls = 0
    def transient_flush(*args, **kwargs):
        nonlocal retry_calls
        retry_calls += 1
        if retry_calls == 1:
            raise store.WriterProjectionError("transient")
        result = original_flush(*args, **kwargs)
        retry_succeeded.set()
        return result
    store.flush_writer_projection = transient_flush
    try:
        retry_ingester.ingest(retry_root, retry_root, "agent_message", {"uuid": "retry"}, source="retry", msg_id="message")
        assert retry_succeeded.wait(3), "background projection did not retry"
    finally:
        store.flush_writer_projection = original_flush
        retry_ingester.shutdown()

    close_ingester = EventIngester()
    close_ingester._mark_fsync_dirty = lambda _root_id: None
    close_root = "close-projection-retention"
    close_ingester.ingest(close_root, close_root, "agent_message", {"uuid": "close"}, source="close", msg_id="message")
    store.flush_writer_projection = lambda *_args, **_kwargs: (_ for _ in ()).throw(store.WriterProjectionError("transient"))
    try:
        close_ingester.close(close_root)
        assert close_root in close_ingester._handles
    finally:
        store.flush_writer_projection = original_flush
    close_ingester.close(close_root)
    assert close_root not in close_ingester._handles

    kill_writer = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from event_ingester import EventIngester
import hydration_index_store as store
root, phase = sys.argv[2], sys.argv[3]
ingester = EventIngester()
if phase == "before-chain":
    ingester._persist_chain_head_locked = lambda *_a, **_k: os.kill(os.getpid(), signal.SIGKILL)
else:
    store.flush_writer_projection = lambda *_a, **_k: os.kill(os.getpid(), signal.SIGKILL)
ingester.ingest(
    root, root, "agent_message", {"uuid": phase},
    source="sigkill-test", msg_id="message",
)
ingester.close(root)
raise AssertionError("failpoint did not terminate process")
'''
    for phase in ("before-chain", "before-projection"):
        kill_root = f"sigkill-{phase}"
        kill_path = Path(HOME) / "sessions" / kill_root / "events.jsonl"
        kill_path.parent.mkdir(parents=True)
        kill_path.write_bytes(_row(kill_root, 1))
        store.load(kill_root, kill_path)
        baseline_size = kill_path.stat().st_size
        killed = subprocess.run([
            sys.executable, "-c", kill_writer,
            str(Path(__file__).resolve().parents[1]), kill_root, phase,
        ], env={**os.environ, "BETTER_AGENT_HOME": HOME})
        assert killed.returncode == -signal.SIGKILL, (phase, killed.returncode)
        with store._receipts_lock:
            store._append_receipts.clear()
        recovered_offsets, recovered_metrics = store.load(kill_root, kill_path)
        assert recovered_metrics["cold"] == 0, (phase, recovered_metrics)
        assert recovered_metrics["scanned_bytes"] == kill_path.stat().st_size - baseline_size
        assert recovered_offsets[kill_root][-1] == baseline_size

    guard_root = "guard-root"
    event_ingester.ingest(
        guard_root, guard_root, "agent_message", {"uuid": "guard-1"},
        source="hydration-index-test", msg_id="message",
    )
    guard_path = event_ingester._events_path(guard_root)
    event_ingester.close(guard_root)
    store.load(guard_root, guard_path)
    with sqlite3.connect(store._db_path(guard_root)) as conn:
        guard_digest = dict(conn.execute("SELECT key, value FROM meta"))["digest"]
    guard_tail = _row(guard_root, 2)
    guard_start = guard_path.stat().st_size
    with guard_path.open("ab") as handle:
        handle.write(guard_tail)
    store.note_authoritative_append(
        guard_root, guard_path, guard_start, guard_path.stat().st_size,
        guard_digest, _next_digest(guard_digest, guard_tail),
    )
    scan_entered = threading.Event()
    scan_release = threading.Event()
    original_scan = store._scan

    def paused_scan(*args, **kwargs):
        scan_entered.set()
        assert scan_release.wait(2)
        return original_scan(*args, **kwargs)

    store._scan = paused_scan
    load_result: list[object] = []
    loader = threading.Thread(
        target=lambda: load_result.append(store.load(guard_root, guard_path)),
    )
    loader.start()
    assert scan_entered.wait(2)
    invalidated = threading.Event()
    invalidator = threading.Thread(
        target=lambda: (store.invalidate(guard_root), invalidated.set()),
    )
    invalidator.start()
    assert not invalidated.wait(0.05)
    scan_release.set()
    loader.join(2)
    invalidator.join(2)
    store._scan = original_scan
    assert load_result and invalidated.is_set()
    assert not store._db_path(guard_root).exists()

    emit_entered = threading.Event()
    emit_release = threading.Event()
    mutation_done = threading.Event()
    original_emit = event_ingester._emit

    def paused_emit(*args, **kwargs):
        emit_entered.set()
        assert emit_release.wait(2)
        return original_emit(*args, **kwargs)

    event_ingester._emit = paused_emit
    writer = threading.Thread(target=lambda: event_ingester.ingest(
        guard_root, guard_root, "agent_message", {"uuid": "guard-3"},
        source="hydration-index-test", msg_id="message",
    ))
    writer.start()
    assert emit_entered.wait(2)

    def guarded_rewrite():
        with store.journal_guard(guard_root):
            payload = bytearray(guard_path.read_bytes())
            payload[payload.index(b"guard-1")] = ord("G")
            guard_path.write_bytes(payload)
        mutation_done.set()

    mutator = threading.Thread(target=guarded_rewrite)
    mutator.start()
    assert not mutation_done.wait(0.05)
    emit_release.set()
    writer.join(2)
    mutator.join(2)
    event_ingester._emit = original_emit
    assert mutation_done.is_set()
    event_ingester.close(guard_root)

    store.load(guard_root, guard_path)
    ready = Path(HOME) / "guard-ready"
    release = Path(HOME) / "guard-release"
    holder_code = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hydration_index_store as store
with store.journal_guard(sys.argv[2]):
    Path(sys.argv[3]).write_text('ready')
    while not Path(sys.argv[4]).exists():
        time.sleep(0.01)
"""
    invalidator_code = """
import sys
sys.path.insert(0, sys.argv[1])
import hydration_index_store as store
store.invalidate(sys.argv[2])
"""
    holder = subprocess.Popen([
        sys.executable, "-c", holder_code, str(Path(__file__).resolve().parents[1]),
        guard_root, str(ready), str(release),
    ], env={**os.environ, "BETTER_AGENT_HOME": HOME})
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    cross_process_invalidator = subprocess.Popen([
        sys.executable, "-c", invalidator_code,
        str(Path(__file__).resolve().parents[1]), guard_root,
    ], env={**os.environ, "BETTER_AGENT_HOME": HOME})
    time.sleep(0.1)
    assert cross_process_invalidator.poll() is None
    release.touch()
    assert holder.wait(timeout=3) == 0
    assert cross_process_invalidator.wait(timeout=3) == 0
    assert not store._db_path(guard_root).exists()

    cold_root = "cold-append-root"
    cold_journal = Path(HOME) / "sessions" / cold_root / "events.jsonl"
    cold_journal.parent.mkdir(parents=True)
    cold_prefix = b"".join(_row(cold_root, value) for value in range(1, 101))
    cold_journal.write_bytes(cold_prefix)
    cold_digest = bytes(32).hex()
    for value in range(1, 101):
        cold_digest = _next_digest(cold_digest, _row(cold_root, value))
    cold_scan_entered = threading.Event()
    cold_scan_release = threading.Event()
    cold_append_done = threading.Event()
    original_scan = store._scan

    def paused_cold_scan(*args, **kwargs):
        if kwargs.get("stop") is not None or len(args) >= 5:
            cold_scan_entered.set()
            assert cold_scan_release.wait(2)
        return original_scan(*args, **kwargs)

    store._discard_pool()
    store.apply_runtime_generation()
    store._pool = ThreadPoolExecutor(max_workers=1)
    store._scan = paused_cold_scan
    cold_result: list[object] = []
    cold_loader = threading.Thread(
        target=lambda: cold_result.append(store.load(cold_root, cold_journal)),
    )
    cold_loader.start()
    assert cold_scan_entered.wait(2)
    appended = _row("appended-during-cold", 101)

    def append_during_cold() -> None:
        with store.journal_guard(cold_root, cold_journal):
            start = cold_journal.stat().st_size
            with cold_journal.open("ab") as handle:
                handle.write(appended)
            store.note_authoritative_append(
                cold_root, cold_journal, start, cold_journal.stat().st_size,
                cold_digest, _next_digest(cold_digest, appended),
            )
        cold_append_done.set()

    cold_writer = threading.Thread(target=append_during_cold)
    cold_writer.start()
    assert cold_append_done.wait(0.5), "cold scan held the journal writer fence"
    cold_scan_release.set()
    cold_loader.join(3)
    cold_writer.join(3)
    store._scan = original_scan
    store._discard_pool()
    assert cold_result
    cold_offsets, cold_metrics = cold_result[0]
    assert cold_metrics["cold"] == 1, cold_metrics
    assert len(cold_offsets[cold_root]) == 100, cold_offsets
    assert cold_offsets["appended-during-cold"] == (len(cold_prefix),), cold_offsets

    journal.write_bytes(_row("replacement", 5))
    offsets, rebuilt = store.load("root", journal)
    assert rebuilt["cold"] == 1 and set(offsets) == {"replacement"}, (offsets, rebuilt)

    target = store._db_path("root")
    target.write_bytes(b"not sqlite")
    offsets, recovered = store.load("root", journal)
    assert recovered["cold"] == 1 and set(offsets) == {"replacement"}

    missing = Path(HOME) / "sessions" / "child-failure" / "events.jsonl"
    try:
        store._publish_cold(missing, store._db_path("child-failure"))
        raise AssertionError("child failure was accepted")
    except RuntimeError:
        pass
    assert not list(store._db_path("child-failure").parent.glob(".*.tmp"))

    first_pool = store._pool
    persistent_target = store._db_path("persistent")
    store._publish_cold(journal, persistent_target)
    assert store._pool is first_pool

    coalesced_target = store._db_path("coalesced")
    failures = []
    def publish_coalesced():
        try:
            store._publish_cold(journal, coalesced_target)
        except BaseException as exc:
            failures.append(exc)
    threads = [
        threading.Thread(target=publish_coalesced)
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures and coalesced_target.exists()
    assert not store._builds

    store.set_generation("generation-2")
    assert store._pool is None
    store._publish_cold(journal, store._db_path("generation-2"))
    assert store._pool is not None and store._pool is not first_pool

    runtime_pool = store._pool
    second_home = tempfile.mkdtemp(prefix="ba-hydration-generation-")
    os.environ["BETTER_AGENT_HOME"] = second_home
    second_journal = Path(second_home) / "sessions" / "runtime-root" / "events.jsonl"
    second_journal.parent.mkdir(parents=True)
    second_journal.write_bytes(_row("runtime", 1))
    offsets, _ = store.load("runtime-root", second_journal)
    assert set(offsets) == {"runtime"}
    assert store._pool is not runtime_pool
    shutil.rmtree(second_home)
    os.environ["BETTER_AGENT_HOME"] = HOME
    store.apply_runtime_generation()

    class BrokenFuture:
        def result(self, timeout=None):
            raise BrokenProcessPool("crashed")

    class BrokenPool:
        def __init__(self):
            self.shutdown_called = False

        def submit(self, *_args):
            return BrokenFuture()

        def shutdown(self, **_kwargs):
            self.shutdown_called = True

    original_new_pool = store._new_pool
    broken_pools = []
    def new_broken_pool():
        pool = BrokenPool()
        broken_pools.append(pool)
        return pool
    store._discard_pool()
    store._new_pool = new_broken_pool
    crash_target = store._db_path("repeated-crash")
    try:
        store._publish_cold(journal, crash_target)
        raise AssertionError("repeated worker crash was accepted")
    except RuntimeError as exc:
        assert "after replacement" in str(exc)
    finally:
        store._new_pool = original_new_pool
    assert len(broken_pools) == 2 and all(pool.shutdown_called for pool in broken_pools)
    assert not store._builds
    assert not list(crash_target.parent.glob(f".{crash_target.name}.*.tmp"))
    store.shutdown()
    assert store._pool is None and not store._builds
    try:
        store._publish_cold(journal, store._db_path("after-shutdown"))
        raise AssertionError("build accepted after shutdown")
    except RuntimeError:
        pass
    store._shutdown.clear()

    large_root = "large-durable-root"
    large_journal = Path(HOME) / "sessions" / large_root / "events.jsonl"
    large_journal.parent.mkdir(parents=True)
    large_size = 1_947_000_000
    with large_journal.open("wb") as handle:
        handle.truncate(large_size)
    large_target = store._db_path(large_root)
    large_conn = store._create(large_target)
    large_stat = large_journal.stat()
    large_rows = 250_937
    large_conn.executemany(
        "INSERT INTO offsets VALUES (?, ?)",
        (("large-sid", index * 7000) for index in range(large_rows)),
    )
    large_conn.executemany("INSERT INTO meta VALUES (?, ?)", {
        "schema": str(store.SCHEMA), "dev": str(large_stat.st_dev),
        "ino": str(large_stat.st_ino), "offset": str(large_size),
        "boundary": store._boundary(large_journal, large_size),
        "mtime_ns": str(large_stat.st_mtime_ns),
        "ctime_ns": str(large_stat.st_ctime_ns), "scanned": "0",
        "rows": str(large_rows), "digest": bytes(32).hex(),
        "reconciled_seq": "0",
    }.items())
    large_conn.commit()
    large_conn.close()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    large_result: list[object] = []
    large_started = time.perf_counter()
    large_loader = threading.Thread(
        target=lambda: large_result.append(store.load(large_root, large_journal)),
    )
    large_loader.start()
    heartbeats = 0
    max_heartbeat_gap = 0.0
    prior_heartbeat = time.perf_counter()
    while large_loader.is_alive():
        heartbeats += 1
        time.sleep(0.05)
        heartbeat = time.perf_counter()
        max_heartbeat_gap = max(max_heartbeat_gap, heartbeat - prior_heartbeat)
        prior_heartbeat = heartbeat
    large_loader.join()
    large_elapsed = time.perf_counter() - large_started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    large_offsets, large_metrics = large_result[0]
    assert len(large_offsets["large-sid"]) == large_rows
    assert large_metrics["scanned_bytes"] == 0, large_metrics
    assert large_elapsed < 15, large_elapsed
    assert heartbeats > 0, heartbeats
    assert max_heartbeat_gap < 0.2, max_heartbeat_gap
    rss_scale = 1024 if sys.platform != "darwin" else 1
    assert (rss_after - rss_before) * rss_scale < 160 * 1024 * 1024

    event_ingester.shutdown()
    assert event_ingester._fsync_thread is None
    print("PASS: hydration index projection is incremental and recoverable")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
