import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
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
    assert not store._processes
    assert not list(store._db_path("child-failure").parent.glob(".*.tmp"))

    original_context = store.multiprocessing.get_context

    class FakeProcess:
        def __init__(self, mode):
            self.mode = mode
            self.exitcode = None
            self.started = False
            self.terminated = False
            self.killed = False
            self.joins = 0
            self.pid = 12345
            self.temp = None

        def start(self):
            if self.mode == "start-raise":
                raise OSError("start failed")
            self.started = True
            if self.temp is not None:
                Path(self.temp).touch()
            if self.mode == "shutdown":
                store._shutdown.set()

        def is_alive(self):
            return self.started and not self.terminated

        def join(self, _timeout=None):
            self.joins += 1

        def terminate(self):
            if self.mode not in {"timeout", "shutdown", "unreapable"}:
                self.terminated = True
                self.exitcode = -15

        def kill(self):
            self.killed = True
            if self.mode != "unreapable":
                self.terminated = True
                self.exitcode = -9

    def run_fake(mode):
        process = FakeProcess(mode)

        class Context:
            def Process(self, **kwargs):
                process.temp = kwargs["args"][1]
                return process

        store._shutdown.clear()
        store.multiprocessing.get_context = lambda _method: Context()
        original_timeout = store.BUILD_TIMEOUT_SECONDS
        store.BUILD_TIMEOUT_SECONDS = 0
        try:
            store._publish_cold(journal, store._db_path(f"fake-{mode}"))
            raise AssertionError(f"{mode} was accepted")
        except (OSError, RuntimeError):
            pass
        finally:
            store.BUILD_TIMEOUT_SECONDS = original_timeout
            store.multiprocessing.get_context = original_context
            store._shutdown.clear()
        if mode != "unreapable":
            assert not store._processes
            assert not list(store._db_path(f"fake-{mode}").parent.glob(".*.tmp"))
        return process

    start_failed = run_fake("start-raise")
    assert not start_failed.started and start_failed.joins == 0
    timed_out = run_fake("timeout")
    assert timed_out.killed and timed_out.joins == 2
    shut_down = run_fake("shutdown")
    assert shut_down.killed and shut_down.joins == 2
    unreapable = run_fake("unreapable")
    assert unreapable in store._processes and unreapable in store._process_temps
    assert store._process_temps[unreapable].exists()
    unreapable.terminated = True
    store.shutdown()
    assert unreapable not in store._processes
    assert not store._process_temps
    store._shutdown.clear()
    print("PASS: hydration index projection is incremental and recoverable")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
