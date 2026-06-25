import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="ba-hydration-index-")
os.environ["BETTER_AGENT_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hydration_index_store as store


def _row(sid: str, value: int, newline: bool = True) -> bytes:
    data = json.dumps({"sid": sid, "seq": value}).encode()
    return data + (b"\n" if newline else b"")


def main() -> int:
    journal = Path(HOME) / "sessions" / "root" / "events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(_row("a", 1))
    offsets, first = store.load("root", journal)
    assert first["cold"] == 1 and offsets["a"] == (0,), (offsets, first)

    original_size = journal.stat().st_size
    append_before = journal.stat()
    with journal.open("ab") as file:
        file.write(_row("b", 2))
    store.note_authoritative_append("root", journal, original_size, journal.stat().st_size, append_before.st_mtime_ns, append_before.st_ctime_ns)
    offsets, appended = store.load("root", journal)
    assert appended["cold"] == 0, appended
    assert appended["scanned_bytes"] == journal.stat().st_size - original_size, appended
    assert offsets["b"] == (original_size,), offsets

    partial_start = journal.stat().st_size
    append_before = journal.stat()
    with journal.open("ab") as file:
        file.write(_row("c", 3, newline=False))
    store.note_authoritative_append("root", journal, partial_start, journal.stat().st_size, append_before.st_mtime_ns, append_before.st_ctime_ns)
    offsets, _ = store.load("root", journal)
    assert "c" not in offsets
    with journal.open("ab") as file:
        file.write(b"\n")
    store.note_authoritative_append("root", journal, partial_start + len(_row("c", 3, newline=False)), journal.stat().st_size)
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
    append_before = journal.stat()
    with journal.open("ab") as file:
        file.write(_row("d", 4))
    store.note_authoritative_append("root", journal, concurrent_start, journal.stat().st_size, append_before.st_mtime_ns, append_before.st_ctime_ns)
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

    rewritten_growth = bytearray(journal.read_bytes())
    rewritten_growth[len(rewritten_growth) // 3] ^= 1
    journal.write_bytes(rewritten_growth)
    growth_start = journal.stat().st_size
    append_before = journal.stat()
    with journal.open("ab") as file:
        file.write(_row("e", 5))
    store.note_authoritative_append(
        "root", journal, growth_start, journal.stat().st_size,
        append_before.st_mtime_ns, append_before.st_ctime_ns,
    )
    _, rewrite_then_append = store.load("root", journal)
    assert rewrite_then_append["cold"] == 1, rewrite_then_append

    journal.write_bytes(_row("replacement", 5))
    offsets, rebuilt = store.load("root", journal)
    assert rebuilt["cold"] == 1 and set(offsets) == {"replacement"}, (offsets, rebuilt)

    target = store._db_path("root")
    target.write_bytes(b"not sqlite")
    offsets, recovered = store.load("root", journal)
    assert recovered["cold"] == 1 and set(offsets) == {"replacement"}

    missing = journal.with_name("missing.jsonl")
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
    second_journal = Path(second_home) / "sessions" / "root" / "events.jsonl"
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
    print("PASS: hydration index projection is incremental and recoverable")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
