"""Unit tests for ``hydration_index_store`` — the sqlite-backed render
hydration projection.

The existing ``test_hydration_index_projection.py`` is a standalone
``main()`` script (pytest collects nothing from it) and exercises the
cold build through a real ``ProcessPoolExecutor`` spawn pool, so the
worker-side functions (``_create``/``_scan``/``_cold_build``) execute in
uninstrumented subprocesses and never appear in unit-tier coverage.

These tests cover that logic IN PROCESS: pure scan/receipt state-machine
logic directly, the worker functions by calling them in-process, the
``_publish_cold`` orchestration branches through fake pools (no real
subprocess), and the ``load`` edge branches. Isolation per test via a
fresh ``ba_home`` and full reset of the module's mutable globals.

Isolation: the module resolves ``ba_home()`` dynamically through its own
module-global reference, so each test monkeypatches ``store.ba_home`` to a
fresh tmp dir. No real home is ever touched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

import hydration_index_store as store


# --- isolation ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ba_home", lambda: tmp_path)
    saved = {
        "_pool": store._pool,
        "_builds": dict(store._builds),
        "_shutdown": store._shutdown,
        "_append_receipts": dict(store._append_receipts),
        "_pool_generation": store._pool_generation,
        "_new_pool": store._new_pool,
    }
    store._pool = None
    store._builds.clear()
    store._append_receipts.clear()
    store._pool_generation = None
    store._shutdown = threading.Event()
    yield
    pool = store._pool
    store._pool = saved["_pool"]
    store._builds.clear()
    store._builds.update(saved["_builds"])
    store._shutdown = saved["_shutdown"]
    store._append_receipts.clear()
    store._append_receipts.update(saved["_append_receipts"])
    store._pool_generation = saved["_pool_generation"]
    store._new_pool = saved["_new_pool"]
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _row(sid: str, seq: int = 1, newline: bool = True) -> bytes:
    data = json.dumps({"sid": sid, "seq": seq}).encode()
    return data + (b"\n" if newline else b"")


def _write_journal(path: Path, rows: list[bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(rows))
    return path


def _inprocess_publish(journal: Path, target: Path) -> None:
    """Cold build without a subprocess — mirrors ``_publish_cold``'s atomic
    temp-then-replace so it works whether or not the target db already
    exists. Used to drive ``load`` cold paths deterministically."""
    import os

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.inprocess.tmp")
    store._cold_build(str(journal), str(temp))
    os.replace(temp, target)


def _seed(root_id: str, journal: Path) -> Path:
    """Build a real cold projection at the module's canonical db path."""
    target = store._db_path(root_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    store._cold_build(str(journal), str(target))
    return target


# --- _db_path ----------------------------------------------------------------


def test_db_path_is_sha256_named_under_cache_dir(tmp_path):
    import hashlib

    path = store._db_path("root-id")
    expected = tmp_path / "cache" / "render-hydration" / (
        hashlib.sha256(b"root-id").hexdigest() + ".sqlite3"
    )
    assert path == expected


# --- _boundary ---------------------------------------------------------------


def test_boundary_clamps_to_zero_below_threshold(tmp_path):
    journal = _write_journal(tmp_path / "j", [b"x" * 10])
    import hashlib

    # offset below BOUNDARY_BYTES clamps start to 0: reads [0, offset).
    assert store._boundary(journal, 5) == hashlib.sha256(b"xxxxx").hexdigest()
    assert store._boundary(journal, 5) == store._boundary(journal, 5)


def test_boundary_hashes_only_last_boundary_bytes(tmp_path):
    payload = b"A" * (store.BOUNDARY_BYTES * 2)
    journal = _write_journal(tmp_path / "j", [payload])
    offset = store.BOUNDARY_BYTES * 2
    import hashlib

    expected = hashlib.sha256(payload[offset - store.BOUNDARY_BYTES:]).hexdigest()
    assert store._boundary(journal, offset) == expected


# --- _create / _meta ---------------------------------------------------------


def test_create_schema_and_empty_meta(tmp_path):
    conn = store._create(tmp_path / "db.sqlite3")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables == {"offsets", "meta"}
    assert store._meta(conn) == {}
    conn.close()


# --- _scan -------------------------------------------------------------------


def _scan_conn(tmp_path):
    conn = store._create(tmp_path / "db.sqlite3")
    return conn


def test_scan_empty_journal_returns_zero(tmp_path):
    journal = _write_journal(tmp_path / "j", [])
    conn = _scan_conn(tmp_path)
    assert store._scan(conn, journal, 0) == (0, 0, 0)
    conn.close()


def test_scan_records_offsets_for_complete_lines(tmp_path):
    journal = _write_journal(
        tmp_path / "j", [_row("a"), _row("b"), _row("a")]
    )
    conn = _scan_conn(tmp_path)
    committed, scanned, inserted = store._scan(conn, journal, 0)
    assert committed == journal.stat().st_size
    assert scanned == journal.stat().st_size
    assert inserted == 3
    rows = conn.execute("SELECT sid, offset FROM offsets ORDER BY offset").fetchall()
    assert rows == [("a", 0), ("b", len(_row("a"))), ("a", len(_row("a")) + len(_row("b")))]
    conn.close()


def test_scan_starts_at_given_offset(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    conn = _scan_conn(tmp_path)
    start = len(_row("a"))
    committed, scanned, _ = store._scan(conn, journal, start)
    assert committed == journal.stat().st_size
    assert scanned == len(_row("b"))
    only = conn.execute("SELECT sid, offset FROM offsets").fetchall()
    assert only == [("b", start)]
    conn.close()


def test_scan_skips_malformed_json_lines(tmp_path):
    journal = _write_journal(
        tmp_path / "j", [b"not json\n", _row("ok"), b"{bad\n"]
    )
    conn = _scan_conn(tmp_path)
    committed, _, inserted = store._scan(conn, journal, 0)
    assert committed == journal.stat().st_size
    assert inserted == 1
    assert conn.execute("SELECT sid FROM offsets").fetchall() == [("ok",)]
    conn.close()


@pytest.mark.parametrize(
    "payload",
    [
        b"123\n",            # non-dict json
        b"[]\n",             # list, no sid
        b'{"sid": null}\n',  # null sid
        b'{"sid": 5}\n',     # non-str sid
        b'{"sid": ""}\n',    # empty sid
        b'{}\n',             # missing sid
    ],
)
def test_scan_skips_rows_without_valid_str_sid(tmp_path, payload):
    journal = _write_journal(tmp_path / "j", [payload, _row("kept")])
    conn = _scan_conn(tmp_path)
    _, _, inserted = store._scan(conn, journal, 0)
    assert inserted == 1
    assert conn.execute("SELECT sid FROM offsets").fetchall() == [("kept",)]
    conn.close()


def test_scan_does_not_commit_incomplete_trailing_line(tmp_path):
    # Final line has no newline -> scanned but not committed/inserted.
    complete = _row("a")
    partial = _row("b", newline=False)
    journal = _write_journal(tmp_path / "j", [complete, partial])
    conn = _scan_conn(tmp_path)
    committed, scanned, inserted = store._scan(conn, journal, 0)
    assert committed == len(complete)
    assert scanned == len(complete) + len(partial)
    assert inserted == 1
    # The partial line offset is NOT recorded.
    assert conn.execute("SELECT count(*) FROM offsets").fetchone()[0] == 1
    conn.close()


def test_scan_flushes_batch_at_threshold(tmp_path):
    # >4096 valid rows forces the executemany flush inside the loop.
    rows = [_row(f"s{i % 50}") for i in range(4097)]
    journal = _write_journal(tmp_path / "j", rows)
    conn = _scan_conn(tmp_path)
    committed, scanned, inserted = store._scan(conn, journal, 0)
    assert committed == journal.stat().st_size
    assert scanned == journal.stat().st_size
    assert inserted == 4097
    conn.close()


# --- _cold_build -------------------------------------------------------------


def test_cold_build_writes_meta_and_offsets(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    out = tmp_path / "out.sqlite3"
    store._cold_build(str(journal), str(out))
    conn = sqlite3.connect(out)
    meta = store._meta(conn)
    assert int(meta["schema"]) == store.SCHEMA
    assert int(meta["offset"]) == journal.stat().st_size
    assert int(meta["rows"]) == 2
    assert int(meta["scanned"]) == journal.stat().st_size
    assert meta["boundary"] == store._boundary(journal, journal.stat().st_size)
    assert conn.execute("SELECT count(*) FROM offsets").fetchone()[0] == 2
    conn.close()


class _StatScript:
    """Duck-typed path: real ``open``/``read``, scripted ``stat`` sequence."""

    def __init__(self, real: Path, stats: list):
        self._real = real
        self._stats = list(stats)

    def stat(self):
        return self._stats.pop(0)

    def open(self, *args, **kwargs):
        return self._real.open(*args, **kwargs)


def test_cold_build_rejects_journal_that_shrunk_during_build(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    real_stat = journal.stat()
    shrunk = type("S", (), {"st_dev": real_stat.st_dev, "st_ino": real_stat.st_ino,
                            "st_size": 0, "st_mtime_ns": 0, "st_ctime_ns": 0})()
    scripted = _StatScript(journal, [real_stat, shrunk])
    monkeypatch.setattr(store, "Path", lambda _p: scripted if _p == str(journal) else Path(_p))
    with pytest.raises(RuntimeError, match="journal changed during hydration index build"):
        store._cold_build(str(journal), str(tmp_path / "out.sqlite3"))


def test_cold_build_rejects_journal_dev_ino_change_during_build(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    first = journal.stat()
    moved = type("S", (), {"st_dev": first.st_dev + 1, "st_ino": first.st_ino + 1,
                           "st_size": first.st_size, "st_mtime_ns": first.st_mtime_ns,
                           "st_ctime_ns": first.st_ctime_ns})()
    scripted = _StatScript(journal, [first, moved])
    monkeypatch.setattr(store, "Path", lambda _p: scripted if _p == str(journal) else Path(_p))
    with pytest.raises(RuntimeError, match="journal changed during hydration index build"):
        store._cold_build(str(journal), str(tmp_path / "out.sqlite3"))


# --- receipt state machine ---------------------------------------------------


def test_note_authoritative_append_records_receipt(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    st = journal.stat()
    store.note_authoritative_append("r", journal, 0, st.st_size)
    assert store._append_receipts["r"][:4] == (st.st_dev, st.st_ino, 0, st.st_size)


def test_note_chains_origin_when_dev_ino_and_start_match(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    first = journal.stat()
    first_end = first.st_size
    store.note_authoritative_append("r", journal, 0, first_end, before_mtime_ns=10, before_ctime_ns=20)
    # Append more on the SAME file (dev/ino unchanged), chaining from first_end.
    second = journal.stat()
    store.note_authoritative_append("r", journal, first_end, second.st_size)
    receipt = store._append_receipts["r"]
    assert receipt[2] == 0          # origin carried over from first append
    assert receipt[4] == 10 and receipt[5] == 20  # origin mtime/ctime carried over


def test_note_resets_origin_when_not_chained(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    st = journal.stat()
    # Different start than any prior receipt -> origin resets to this start.
    store.note_authoritative_append("r", journal, 999, st.st_size)
    assert store._append_receipts["r"][2] == 999


def test_growth_is_authoritative_requires_matching_receipt(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    st = journal.stat()
    meta = {"mtime_ns": str(st.st_mtime_ns), "ctime_ns": str(st.st_ctime_ns)}
    # No receipt -> not authoritative.
    assert store._growth_is_authoritative("r", meta, st, 0) is False
    store.note_authoritative_append("r", journal, 0, st.st_size)
    assert store._growth_is_authoritative("r", meta, st, 0) is True


def test_growth_is_authoritative_false_on_mtime_mismatch(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    st = journal.stat()
    store.note_authoritative_append("r", journal, 0, st.st_size)
    meta = {"mtime_ns": str(st.st_mtime_ns + 1), "ctime_ns": str(st.st_ctime_ns)}
    assert store._growth_is_authoritative("r", meta, st, 0) is False


def test_acknowledge_pops_when_committed_equals_end(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    st = journal.stat()
    store.note_authoritative_append("r", journal, 0, st.st_size)
    store._acknowledge_growth("r", st.st_size, st)
    assert "r" not in store._append_receipts


def test_acknowledge_shrinks_when_committed_inside_range(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b"), _row("c")])
    st = journal.stat()
    end = st.st_size
    store.note_authoritative_append("r", journal, 0, end)
    midway = len(_row("a"))
    store._acknowledge_growth("r", midway, st)
    receipt = store._append_receipts["r"]
    assert receipt[2] == midway and receipt[3] == end


def test_acknowledge_noop_when_committed_out_of_range(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    st = journal.stat()
    store.note_authoritative_append("r", journal, 5, 10)
    # committed beyond receipt end -> no-op.
    store._acknowledge_growth("r", 999, st)
    assert store._append_receipts["r"][2] == 5 and store._append_receipts["r"][3] == 10


def _seed_valid_meta(tmp_path):
    """Build a real cold projection; return (journal, meta)."""
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    out = _seed("r", journal)
    conn = sqlite3.connect(out)
    meta = store._meta(conn)
    conn.close()
    return journal, meta


def test_valid_append_false_on_schema_mismatch(tmp_path):
    journal, meta = _seed_valid_meta(tmp_path)
    meta["schema"] = "999"
    assert store._valid_append("r", meta, journal) is False


def test_valid_append_true_for_fresh_projection(tmp_path):
    journal, meta = _seed_valid_meta(tmp_path)
    assert store._valid_append("r", meta, journal) is True


def test_valid_append_false_on_boundary_mismatch(tmp_path):
    journal, meta = _seed_valid_meta(tmp_path)
    meta["boundary"] = "deadbeef"
    assert store._valid_append("r", meta, journal) is False


def test_valid_append_exact_size_needs_matching_mtime_ctime(tmp_path):
    journal, meta = _seed_valid_meta(tmp_path)
    assert store._valid_append("r", meta, journal) is True
    # Change mtime without changing content -> size==offset path rejects.
    import os

    os.utime(journal, ns=(journal.stat().st_atime_ns + 10_000_000, journal.stat().st_mtime_ns + 10_000_000))
    assert store._valid_append("r", meta, journal) is False


def test_valid_append_growth_authoritative_only_with_receipt(tmp_path):
    journal, meta = _seed_valid_meta(tmp_path)
    # Grow the journal without rebuilding -> size > offset, base valid.
    with journal.open("ab") as f:
        f.write(_row("c"))
    assert store._valid_append("r", meta, journal) is False
    # Receipt's origin mtime/ctime must match the projection's recorded
    # mtime/ctime (captured before the append) for growth to be authoritative.
    store.note_authoritative_append(
        "r", journal, int(meta["offset"]), journal.stat().st_size,
        before_mtime_ns=int(meta["mtime_ns"]), before_ctime_ns=int(meta["ctime_ns"]),
    )
    assert store._valid_append("r", meta, journal) is True


# --- _publish_cold orchestration (fake pools, no real subprocess) ------------


class _FakePool:
    def __init__(self, submit_fn):
        self._submit_fn = submit_fn
        self.shutdown_called = False

    def submit(self, fn, *args):
        return self._submit_fn(fn, args)

    def shutdown(self, **kwargs):
        self.shutdown_called = True


def _done(result=None):
    f = Future()
    f.set_result(result)
    return f


class _RaisingFuture:
    def __init__(self, exc):
        self._exc = exc

    def result(self, timeout=None):
        raise self._exc


def test_publish_cold_success_replaces_temp(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"
    store._pool = _FakePool(lambda fn, args: (fn(*args), _done())[1])
    store._publish_cold(journal, target)
    assert target.exists()
    assert not store._builds
    # No stray temp files.
    assert not list(target.parent.glob(".*.tmp"))


def test_publish_cold_coalesces_duplicate_target(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"
    release = threading.Event()
    started = threading.Event()

    def submit_fn(fn, args):
        started.set()
        # First submit blocks until released so a second publish sees the
        # in-flight build and coalesces onto it.
        release.wait(5)
        fn(*args)
        return _done()

    store._pool = _FakePool(submit_fn)
    errors = []

    def publish():
        try:
            store._publish_cold(journal, target)
        except BaseException as exc:
            errors.append(exc)

    main_holder = threading.Thread(target=publish)
    main_holder.start()
    assert started.wait(2)
    # Second concurrent publish should coalesce (reuse the in-flight build).
    coalescer = threading.Thread(target=publish)
    coalescer.start()
    release.set()
    main_holder.join(5)
    coalescer.join(5)
    assert not errors
    assert target.exists()


def test_publish_cold_failure_pops_build_and_unlinks_temp(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"
    store._pool = _FakePool(lambda fn, args: _RaisingFuture(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="worker build failed"):
        store._publish_cold(journal, target)
    assert not target.exists()
    assert not store._builds
    assert not list(target.parent.glob(".*.tmp"))


def test_publish_cold_timeout_discards_pool_and_raises(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"
    pool = _FakePool(lambda fn, args: _RaisingFuture(FutureTimeoutError()))
    store._pool = pool
    with pytest.raises(RuntimeError, match="timed out"):
        store._publish_cold(journal, target)
    assert pool.shutdown_called
    assert store._pool is None


def test_publish_cold_broken_pool_replaces_then_raises(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"
    pools = []

    def new_pool():
        p = _FakePool(lambda fn, args: _RaisingFuture(BrokenProcessPool("x")))
        pools.append(p)
        return p

    store._pool = None
    store._new_pool = new_pool
    with pytest.raises(RuntimeError, match="after replacement"):
        store._publish_cold(journal, target)
    assert len(pools) == 2 and all(p.shutdown_called for p in pools)
    assert store._pool is None
    assert not list(target.parent.glob(".*.tmp"))


def test_publish_cold_shutdown_guard_before_submit(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    store._shutdown.set()
    with pytest.raises(RuntimeError, match="shutting down"):
        store._publish_cold(journal, tmp_path / "out.sqlite3")


def test_publish_cold_cancelled_by_shutdown_after_submit(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"

    class _ShutdownFuture:
        def result(self, timeout=None):
            store._shutdown.set()
            return None

    store._pool = _FakePool(lambda fn, args: _ShutdownFuture())
    with pytest.raises(RuntimeError, match="cancelled by shutdown"):
        store._publish_cold(journal, target)
    assert not target.exists()


# --- pool lifecycle: set_generation / discard / shutdown ---------------------


def test_set_generation_noop_when_same():
    store._pool_generation = "gen-1"
    store.set_generation("gen-1")
    assert store._pool_generation == "gen-1"


def test_set_generation_discards_pool_when_changed():
    pool = _FakePool(lambda fn, args: _done())
    store._pool = pool
    store._pool_generation = "gen-1"
    store.set_generation("gen-2")
    assert pool.shutdown_called
    assert store._pool is None
    assert store._pool_generation == "gen-2"


def test_discard_pool_unlinks_stale_temp_builds(tmp_path):
    temp = tmp_path / ".staged.tmp"
    temp.write_bytes(b"x")
    target = tmp_path / "real.sqlite3"
    pool = _FakePool(lambda fn, args: _done())
    store._pool = pool
    store._builds[target] = (_done(), temp)
    store._discard_pool()
    assert pool.shutdown_called
    assert store._pool is None
    assert not store._builds
    assert not temp.exists()


def test_shutdown_sets_flag_discards_pool_and_clears_builds(tmp_path):
    temp = tmp_path / ".staged.tmp"
    temp.write_bytes(b"x")
    pool = _FakePool(lambda fn, args: _done())
    store._pool = pool
    store._builds[tmp_path / "t.sqlite3"] = (_done(), temp)
    store.shutdown()
    assert store._shutdown.is_set()
    assert store._pool is None
    assert not store._builds
    assert not temp.exists()


def test_publish_after_shutdown_raises(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    store.shutdown()
    with pytest.raises(RuntimeError, match="shutting down"):
        store._publish_cold(journal, tmp_path / "out.sqlite3")


# --- load: warm path, incremental, merge, cold branches ----------------------


def test_load_warm_path_returns_offsets_and_metrics(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    _seed("r", journal)
    offsets, metrics = store.load("r", journal)
    assert metrics["cold"] == 0
    assert offsets["a"] == (0,)
    assert offsets["b"] == (len(_row("a")),)
    assert metrics["rows"] == 2
    assert metrics["checkpoint"] == journal.stat().st_size


def test_load_incremental_append_without_rescan(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    _seed("r", journal)
    base = journal.stat().st_size
    before = journal.stat()
    with journal.open("ab") as f:
        f.write(_row("b"))
    store.note_authoritative_append("r", journal, base, journal.stat().st_size,
                                    before_mtime_ns=before.st_mtime_ns, before_ctime_ns=before.st_ctime_ns)
    offsets, metrics = store.load("r", journal)
    assert metrics["cold"] == 0
    assert metrics["scanned_bytes"] == len(_row("b"))
    assert offsets["b"] == (base,)


def test_load_merges_base_offsets_when_checkpoint_within_range(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    _seed("r", journal)
    base_offsets = {"a": (999,)}          # prior knowledge to merge
    base_checkpoint = 0
    offsets, metrics = store.load("r", journal, base_offsets, base_checkpoint)
    assert metrics["cold"] == 0
    assert offsets["a"] == (999, 0)        # merged with projection's own offset


def test_load_skips_merge_when_base_checkpoint_too_high(tmp_path):
    journal = _write_journal(tmp_path / "j", [_row("a"), _row("b")])
    _seed("r", journal)
    offsets, metrics = store.load("r", journal, {"a": (999,)}, base_checkpoint=10**9)
    assert metrics["cold"] == 0
    # base_offsets ignored entirely.
    assert offsets["a"] == (0,)


def test_load_cold_path_when_no_db_exists(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    monkeypatch.setattr(store, "_publish_cold", _inprocess_publish)
    offsets, metrics = store.load("r", journal)
    assert metrics["cold"] == 1
    assert offsets["a"] == (0,)


def test_load_cold_path_when_projection_became_stale(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    _seed("r", journal)
    # Mutate content -> boundary mismatch -> stale -> cold rebuild.
    mutated = bytearray(journal.read_bytes())
    mutated[0] ^= 1
    journal.write_bytes(mutated)
    monkeypatch.setattr(store, "_publish_cold", _inprocess_publish)
    offsets, metrics = store.load("r", journal)
    # Boundary mismatch forced a cold rebuild; the corrupted first byte makes
    # the single line unparseable JSON, so the rebuild yields no sid offsets.
    assert metrics["cold"] == 1
    assert offsets == {}


def test_load_re_publishes_when_second_validity_check_fails(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    _seed("r", journal)
    real_valid = store._valid_append
    calls = {"n": 0}

    def flaky(root_id, meta, j):
        calls["n"] += 1
        # First (pre-txn) check passes; second (in-txn) check fails -> cold re-publish.
        return real_valid(root_id, meta, j) if calls["n"] == 1 else False

    monkeypatch.setattr(store, "_valid_append", flaky)
    monkeypatch.setattr(store, "_publish_cold", _inprocess_publish)
    offsets, metrics = store.load("r", journal)
    assert calls["n"] >= 2
    assert metrics["cold"] == 1
    assert offsets["a"] == (0,)


def test_load_rolls_back_on_scan_failure(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    _seed("r", journal)
    # Append and record an authoritative receipt so the warm path stays valid
    # and load reaches the in-transaction incremental scan (where _scan runs).
    base = journal.stat().st_size
    before = journal.stat()
    with journal.open("ab") as f:
        f.write(_row("b"))
    store.note_authoritative_append(
        "r", journal, base, journal.stat().st_size,
        before_mtime_ns=before.st_mtime_ns, before_ctime_ns=before.st_ctime_ns,
    )

    def boom(conn, j, start):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(store, "_scan", boom)
    with pytest.raises(RuntimeError, match="scan failed"):
        store.load("r", journal)
    # The projection DB is still usable afterwards (rollback released the txn).
    conn = sqlite3.connect(store._db_path("r"))
    assert conn.execute("SELECT count(*) FROM offsets").fetchone()[0] == 1
    conn.close()


# --- remaining defensive branches -------------------------------------------


def test_new_pool_constructs_real_executor_with_configured_workers():
    # Construction does not spawn workers (spawn workers start on first submit),
    # so this covers _new_pool without a real subprocess.
    pool = store._new_pool()
    try:
        assert pool._max_workers == store._WORKER_COUNT
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_publish_cold_in_lock_shutdown_guard(tmp_path, monkeypatch):
    # First guard (line 170) passes; shutdown lands between the two guards so
    # the in-lock guard (line 175-176) fires.
    journal = _write_journal(tmp_path / "j", [_row("a")])

    class _ToggleShutdown:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n >= 2

        def set(self):
            pass

        def clear(self):
            pass

    monkeypatch.setattr(store, "_shutdown", _ToggleShutdown())
    store._pool = _FakePool(lambda fn, args: _done())
    with pytest.raises(RuntimeError, match="shutting down"):
        store._publish_cold(journal, tmp_path / "out.sqlite3")


def test_publish_cold_failure_when_build_already_cleared(tmp_path):
    # The build entry is removed before the except runs -> the pop branch is
    # skipped but the temp is still unlinked and the error propagated.
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = tmp_path / "out.sqlite3"

    class _ClearingFuture:
        def result(self, timeout=None):
            store._builds.clear()
            raise RuntimeError("boom")

    store._pool = _FakePool(lambda fn, args: _ClearingFuture())
    with pytest.raises(RuntimeError, match="worker build failed"):
        store._publish_cold(journal, target)
    assert not target.exists()


def test_shutdown_unlinks_leftover_builds(tmp_path, monkeypatch):
    # shutdown() must clean up temp builds even when _discard_pool did not
    # (a concurrent path could repopulate _builds between the two calls).
    temp = tmp_path / ".leftover.tmp"
    temp.write_bytes(b"x")
    monkeypatch.setattr(store, "_discard_pool", lambda: None)
    store._builds[tmp_path / "t.sqlite3"] = (_done(), temp)
    store.shutdown()
    assert not temp.exists()


def test_load_cold_path_when_db_file_corrupt(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    target = store._db_path("r")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not sqlite")  # connect succeeds; first query raises
    monkeypatch.setattr(store, "_publish_cold", _inprocess_publish)
    offsets, metrics = store.load("r", journal)
    assert metrics["cold"] == 1
    assert offsets["a"] == (0,)


def test_load_reraises_when_in_transaction_republish_fails(tmp_path, monkeypatch):
    journal = _write_journal(tmp_path / "j", [_row("a")])
    _seed("r", journal)
    real_valid = store._valid_append
    calls = {"n": 0}

    def flaky(rid, meta, j):
        calls["n"] += 1
        return real_valid(rid, meta, j) if calls["n"] == 1 else False

    def failing_publish(journal, target):
        raise RuntimeError("republish failed")

    monkeypatch.setattr(store, "_valid_append", flaky)
    monkeypatch.setattr(store, "_publish_cold", failing_publish)
    with pytest.raises(RuntimeError, match="republish failed"):
        store.load("r", journal)
