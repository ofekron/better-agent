"""Unit-tier owner test for ``root_change_wal.py``.

The standalone ``test_root_change_wal.py`` covers the durability flows
(bootstrap diff, batching, crash replay, failure fencing). This file closes
the unit-tier gaps it leaves open: WAL-level validation/error branches, owner
lifecycle edges (idempotent start, ready/shutdown timeouts, the observation
API), the local-mutation API and its error release path, ``replay_once``
rejection, ``poll_once``/``pending_count`` defensive branches (exercised via
targeted OSError injection — legitimate error injection for defensive branches
that are otherwise non-deterministic on a healthy POSIX filesystem), the
background ``_run`` tick loop, and ``_disk_snapshot``/``_signature`` OSError
branches.

The shared ``conftest.py`` autouse fixture engages an isolated
``BETTER_AGENT_HOME`` tempdir per test, so no real home state is touched.
``tmp_path`` is used for the WAL files themselves since the WAL takes an
explicit path.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import root_change_wal
from root_change_wal import RootChange, RootChangeOwner, RootChangeWal


@pytest.fixture
def wal(tmp_path: Path) -> RootChangeWal:
    opened = RootChangeWal(tmp_path / "wal.sqlite3")
    opened.open()
    return opened


def _owner(
    path: Path,
    roots: tuple[Path, ...],
    apply,
    *,
    wal_cls: type[RootChangeWal] = RootChangeWal,
    **kwargs,
) -> RootChangeOwner:
    return RootChangeOwner(
        wal=wal_cls(path),
        roots=lambda: roots,
        apply=apply,
        poll_interval_s=kwargs.pop("poll_interval_s", 60),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# RootChangeWal validation and error branches
# --------------------------------------------------------------------------- #

def test_open_is_idempotent(wal: RootChangeWal):
    conn_before = wal._connection
    wal.open()  # second open must be a no-op
    assert wal._connection is conn_before
    wal.append("upsert", "r", Path("a.json"), (1, 2, 3, 4, 5))
    wal.close()


def test_append_single_wraps_append_many(wal: RootChangeWal):
    seq = wal.append("upsert", "root-a", Path("a.json"), (1, 2, 3, 4, 5))
    assert isinstance(seq, int)
    assert seq >= 1
    rows = wal.read_after(0, 10)
    assert len(rows) == 1 and rows[0].seq == seq
    wal.close()


def test_append_many_empty_returns_empty(wal: RootChangeWal):
    assert wal.append_many([]) == []
    wal.close()


@pytest.mark.parametrize("bad_kind", ["create", "", "UPsert"])
def test_append_many_rejects_unknown_kind(wal: RootChangeWal, bad_kind):
    with pytest.raises(ValueError, match="unsupported root change kind"):
        wal.append_many([(bad_kind, "r", Path("a.json"), None)])
    wal.close()


@pytest.mark.parametrize("bad_root", ["", "a/b", "."])
def test_append_many_rejects_invalid_root_id(wal: RootChangeWal, bad_root):
    with pytest.raises(ValueError, match="root_id must be a non-empty"):
        wal.append_many([("upsert", bad_root, Path("a.json"), None)])
    wal.close()


def test_append_many_accepts_dotdot_root_id(wal: RootChangeWal):
    # `..` is a single path segment (Path("..").name == ".."), so the WAL
    # accepts it. This pins the validation boundary against a regression.
    rows = wal.append_many([("upsert", "..", Path("a.json"), None)])
    assert rows[0].root_id == ".."
    wal.close()


def test_read_after_rejects_invalid_cursor_or_limit(wal: RootChangeWal):
    with pytest.raises(ValueError, match="invalid WAL cursor or limit"):
        wal.read_after(-1, 10)
    with pytest.raises(ValueError, match="invalid WAL cursor or limit"):
        wal.read_after(0, 0)
    wal.close()


def test_require_connection_raises_when_not_open(tmp_path: Path):
    unopened = RootChangeWal(tmp_path / "x.sqlite3")
    with pytest.raises(RuntimeError, match="not open"):
        unopened.read_after(0, 1)


def test_operations_raise_after_close(wal: RootChangeWal):
    wal.close()
    with pytest.raises(RuntimeError, match="not open"):
        wal.append_many([("upsert", "r", Path("a.json"), None)])


def test_commit_projection_empty_advances_nothing(wal: RootChangeWal):
    consumer = "c"
    assert wal.checkpoint(consumer) == 0
    wal.commit_projection(consumer, [])
    assert wal.checkpoint(consumer) == 0
    wal.close()


def test_commit_projection_upsert_then_delete_removes_signature(wal: RootChangeWal):
    consumer = "c"
    upserted = wal.append_many([("upsert", "root-a", Path("a.json"), (1, 2, 3, 4, 5))])[0]
    wal.commit_projection(consumer, [upserted])
    assert wal.owner_signatures(consumer) == {Path("a.json"): ("root-a", (1, 2, 3, 4, 5))}
    deleted = RootChange(upserted.seq, "delete", "root-a", Path("a.json"), None)
    wal.commit_projection(consumer, [deleted])
    assert wal.owner_signatures(consumer) == {}
    wal.close()


def test_commit_projection_upsert_updates_existing_signature(wal: RootChangeWal):
    consumer = "c"
    first = wal.append_many([("upsert", "root-a", Path("a.json"), (1, 2, 3, 4, 5))])[0]
    wal.commit_projection(consumer, [first])
    refreshed = RootChange(first.seq, "upsert", "root-a", Path("a.json"), (9, 9, 9, 9, 9))
    wal.commit_projection(consumer, [refreshed])
    assert wal.owner_signatures(consumer)[Path("a.json")] == ("root-a", (9, 9, 9, 9, 9))
    wal.close()


def test_commit_projection_upsert_without_signature_skips_insert(wal: RootChangeWal):
    consumer = "c"
    sigless = RootChange(1, "upsert", "root-a", Path("a.json"), None)
    wal.commit_projection(consumer, [sigless])
    # checkpoint advances but no owner signature row is written
    assert wal.owner_signatures(consumer) == {}
    assert wal.checkpoint(consumer) == 1
    wal.close()


def test_read_after_round_trips_appended_changes(wal: RootChangeWal):
    wal.append_many((
        ("upsert", "root-a", Path("a.json"), (1, 2, 3, 4, 5)),
        ("delete", "root-a", Path("b.json"), None),
    ))
    changes = wal.read_after(0, 10)
    assert [(c.kind, c.root_id, c.signature) for c in changes] == [
        ("upsert", "root-a", (1, 2, 3, 4, 5)),
        ("delete", "root-a", None),
    ]
    wal.close()


# --------------------------------------------------------------------------- #
# RootChangeOwner construction and lifecycle
# --------------------------------------------------------------------------- #

def test_owner_rejects_nonpositive_bounds(tmp_path: Path):
    wal_path = tmp_path / "w.sqlite3"
    with pytest.raises(ValueError, match="watcher bounds must be positive"):
        RootChangeOwner(
            wal=RootChangeWal(wal_path), roots=lambda: (), apply=lambda c: None,
            max_entries_per_tick=0,
        )
    with pytest.raises(ValueError, match="watcher bounds must be positive"):
        RootChangeOwner(
            wal=RootChangeWal(wal_path), roots=lambda: (), apply=lambda c: None,
            poll_interval_s=0,
        )


def test_start_is_idempotent(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start()
    thread = o._thread
    o.start()  # no-op: already running
    assert o._thread is thread
    o.stop()


def test_wait_ready_times_out_when_not_started(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    with pytest.raises(TimeoutError, match="readiness timed out"):
        o.wait_ready(timeout=0.01)


def test_stop_when_never_started_closes_wal(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.stop()  # no thread, no scanner, unopened wal -> must not raise


class _BlockingOwner(RootChangeOwner):
    """Owner whose background thread ignores ``_stop`` until released."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._block = threading.Event()

    def _run(self) -> None:
        self._block.wait(5.0)


class _FakeDirEntry:
    """Minimal ``os.DirEntry`` stand-in for OSError injection."""

    def __init__(self, path: Path, *, stat_boom: bool = False):
        self.path = str(path)
        self.name = path.name
        self._stat_boom = stat_boom

    def is_file(self):
        return True

    def stat(self):
        if self._stat_boom:
            raise OSError("stat io")
        return Path(self.path).stat()


class _FakeScanner:
    """``os.ScandirIterator`` stand-in: iterable + close + context manager."""

    def __init__(self, entries):
        self._it = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self._it = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _NextBoomScanner(_FakeScanner):
    """Scanner whose ``__next__`` always raises ``OSError``."""

    def __init__(self):
        super().__init__([])

    def __next__(self):
        raise OSError("io")


def test_stop_times_out_when_thread_will_not_exit(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _BlockingOwner(
        wal=RootChangeWal(tmp_path / "w.sqlite3"),
        roots=lambda: (sessions,),
        apply=lambda c: None,
        poll_interval_s=60,
    )
    o.start()
    with pytest.raises(TimeoutError, match="shutdown timed out"):
        o.stop(timeout=0.05)
    assert o._thread is not None  # restored after the timeout
    o._block.set()                 # release the blocked thread
    o._thread.join(2.0)
    assert not o._thread.is_alive()
    o.stop(timeout=1.0)            # clean teardown: unregister queue, close wal


def test_observation_generation_and_wait_for_observation(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start()
    o.wait_ready(3)
    gen = o.observation_generation
    assert gen >= 1
    assert o.wait_for_observation(gen - 1, 1.0) is True     # already satisfied
    assert o.wait_for_observation(gen + 100, 0.05) is False  # unreachable -> timeout
    o.stop()


# --------------------------------------------------------------------------- #
# Local mutation API
# --------------------------------------------------------------------------- #

def test_begin_local_upsert_missing_file_releases_lock(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    with pytest.raises(FileNotFoundError):
        o.begin_local_upsert("r", sessions / "ghost.json")
    assert o._operation_lock.acquire(blocking=False)  # lock was released
    o._operation_lock.release()
    o.stop()


def test_begin_local_upsert_signature_oserror_becomes_filenotfound(tmp_path: Path, monkeypatch):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    target = sessions / "a.json"
    target.write_text("{}")
    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self == target:
            raise OSError("stat io")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    with pytest.raises(FileNotFoundError):
        o.begin_local_upsert("r", target)
    assert o._operation_lock.acquire(blocking=False)
    o._operation_lock.release()
    o.stop()


def test_begin_local_delete_and_complete_local_delete(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    target = sessions / "del.json"
    target.write_text("{}")

    upserted = o.begin_local_upsert("r", target)
    o.complete_local(upserted)
    assert target in o._known

    deletion = o.begin_local_delete("r", target)
    assert deletion.kind == "delete"
    o.complete_local(deletion)
    assert target not in o._known
    o.stop()


def test_begin_local_delete_releases_lock_when_append_fails(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    o._wal.close()  # append_many will raise RuntimeError (wal not open)
    with pytest.raises(RuntimeError):
        o.begin_local_delete("r", sessions / "x.json")
    assert o._operation_lock.acquire(blocking=False)  # lock released despite failure
    o._operation_lock.release()
    o.stop()


def test_abandon_local_releases_operation_lock(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    o._operation_lock.acquire()
    o.abandon_local()
    assert o._operation_lock.acquire(blocking=False)
    o._operation_lock.release()
    o.stop()


def test_complete_local_sigless_upsert_updates_nothing(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    target = sessions / "x.json"; target.write_text("{}")
    o._operation_lock.acquire()
    # A non-delete change with no signature neither pops nor sets known.
    o.complete_local(RootChange(1, "upsert", "r", target, None))
    assert target not in o._known
    assert o._operation_lock.acquire(blocking=False)  # lock released in finally
    o._operation_lock.release()
    o.stop()


# --------------------------------------------------------------------------- #
# replay_once
# --------------------------------------------------------------------------- #

def test_replay_once_rejects_when_apply_returns_false(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    pre = RootChangeWal(tmp_path / "w.sqlite3")
    pre.open()
    pre.append_many((("upsert", "r", Path("a.json"), (1, 2, 3, 4, 5)),))
    pre.close()
    o = _owner(tmp_path / "w.sqlite3", (), lambda c: False)
    o.start()
    with pytest.raises(RuntimeError, match="startup failed"):
        o.wait_ready(3)
    o.stop()


def test_replay_once_updates_known_for_upsert_and_delete(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    keep = sessions / "keep.json"
    keep.write_text("{}")            # present on disk so it survives reconciliation
    pre = RootChangeWal(tmp_path / "w.sqlite3")
    pre.open()
    pre.append_many((
        ("upsert", "r", Path("keep.json"), (1, 2, 3, 4, 5)),
        ("delete", "r", Path("gone.json"), None),       # no-op pop -> delete branch
        ("upsert", "r", Path("sigless.json"), None),    # signature None -> elif-skip branch
    ))
    pre.close()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    assert keep in o._known               # upserted, retained on disk
    assert Path("gone.json") not in o._known
    o.stop()


# --------------------------------------------------------------------------- #
# pending_count
# --------------------------------------------------------------------------- #

def test_pending_count_reflects_unreplayed_changes(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    pre = RootChangeWal(tmp_path / "w.sqlite3")
    pre.open()
    pre.append_many((("upsert", "r", Path("a.json"), (1, 2, 3, 4, 5)),))
    pre.close()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    assert o.pending_count() == 0                      # bootstrap consumed it
    o._wal.append("upsert", "r", Path("late.json"), (1, 1, 1, 1, 1))
    assert o.pending_count() == 1
    o.stop()


def test_pending_count_swallows_runtime_error_when_wal_closed(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    o._wal.close()  # checkpoint/read_after now raise RuntimeError -> caught -> 0
    assert o.pending_count() == 0
    o.stop()


# --------------------------------------------------------------------------- #
# poll_once defensive branches (error injection)
# --------------------------------------------------------------------------- #

def test_poll_once_skips_unscannable_directory(tmp_path: Path):
    sessions = tmp_path / "s"; sessions.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (sessions,), lambda c: None)
    o.start(); o.wait_ready(3)
    o._cycle_dirs = ()                       # force re-init from roots
    o._roots = lambda: (tmp_path / "missing",)  # scandir -> OSError -> skipped
    assert o.poll_once() == 0
    o.stop()


def test_poll_once_skips_non_files_and_unaccepted_paths(tmp_path: Path):
    d = tmp_path / "d"; d.mkdir()
    (d / "sub").mkdir()              # directory -> not a file
    (d / "notes.txt").write_text("x")  # wrong suffix -> not accepted
    (d / "ok.json").write_text("{}")
    o = _owner(tmp_path / "w.sqlite3", (d,), lambda c: None, max_entries_per_tick=10)
    o.start(); o.wait_ready(3)
    o._cycle_dirs = ()
    o.poll_once()
    assert (d / "ok.json") in o._known   # only the json survived the accept filter
    o.stop()


def test_poll_once_handles_scandir_next_oserror(tmp_path: Path, monkeypatch):
    d = tmp_path / "d"; d.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (d,), lambda c: None, max_entries_per_tick=5)
    o.start(); o.wait_ready(3)
    monkeypatch.setattr(root_change_wal.os, "scandir", lambda p: _NextBoomScanner())
    o._cycle_dirs = ()
    assert o.poll_once() == 0     # next() raised -> directory skipped
    o.stop()


def test_poll_once_handles_entry_stat_oserror(tmp_path: Path, monkeypatch):
    d = tmp_path / "d"; d.mkdir()
    o = _owner(tmp_path / "w.sqlite3", (d,), lambda c: None, max_entries_per_tick=5)
    o.start(); o.wait_ready(3)

    def fake_scandir(_directory):
        return _FakeScanner([_FakeDirEntry(d / "x.json", stat_boom=True)])

    monkeypatch.setattr(root_change_wal.os, "scandir", fake_scandir)
    o._cycle_dirs = ()
    assert o.poll_once() == 1     # entry counted, then stat failed -> skipped from snapshot
    o.stop()


# --------------------------------------------------------------------------- #
# Background _run tick loop
# --------------------------------------------------------------------------- #

def test_background_loop_picks_up_new_file(tmp_path: Path):
    d = tmp_path / "d"; d.mkdir()
    applied: list[RootChange] = []
    o = RootChangeOwner(
        wal=RootChangeWal(tmp_path / "w.sqlite3"), roots=lambda: (d,),
        apply=applied.append, max_entries_per_tick=100, poll_interval_s=0.02,
    )
    o.start(); o.wait_ready(3)
    (d / "late.json").write_text("{}")
    deadline = time.monotonic() + 3.0
    while not applied and time.monotonic() < deadline:
        time.sleep(0.01)
    assert applied                      # a background tick observed + applied it
    assert applied[0].root_id == "late"
    o.stop()


def test_background_loop_swallows_poll_exception(tmp_path: Path, monkeypatch):
    d = tmp_path / "d"; d.mkdir()
    o = RootChangeOwner(
        wal=RootChangeWal(tmp_path / "w.sqlite3"), roots=lambda: (d,),
        apply=lambda c: None, max_entries_per_tick=100, poll_interval_s=0.02,
    )
    o.start(); o.wait_ready(3)
    real_poll = o.poll_once
    state = {"boom": True}

    def maybe_boom():
        if state["boom"]:
            raise RuntimeError("tick boom")
        return real_poll()

    monkeypatch.setattr(o, "poll_once", maybe_boom)
    time.sleep(0.12)                    # background loop ticks, raises, swallows, survives
    assert o._thread is not None and o._thread.is_alive()
    state["boom"] = False               # stop raising so teardown is clean
    o.stop()


# --------------------------------------------------------------------------- #
# _disk_snapshot defensive branches (error injection)
# --------------------------------------------------------------------------- #

def test_disk_snapshot_skips_missing_dir_and_non_files(tmp_path: Path):
    real = tmp_path / "real"; real.mkdir()
    (real / "sub").mkdir()              # non-file -> skipped
    (real / "ok.json").write_text("{}")
    missing = tmp_path / "missing"      # scandir -> OSError -> skipped
    o = _owner(tmp_path / "w.sqlite3", (missing, real), lambda c: None)
    o.start(); o.wait_ready(3)
    assert Path(real / "ok.json") in o._known
    o.stop()


def test_disk_snapshot_skips_entry_stat_oserror(tmp_path: Path, monkeypatch):
    real = tmp_path / "real"; real.mkdir()
    (real / "ok.json").write_text("{}")
    real_scandir = root_change_wal.os.scandir

    def fake_scandir(directory):
        entries = [_FakeDirEntry(Path(e.path), stat_boom=True) for e in real_scandir(directory)]
        return _FakeScanner(entries)

    monkeypatch.setattr(root_change_wal.os, "scandir", fake_scandir)
    o = _owner(tmp_path / "w.sqlite3", (real,), lambda c: None)
    o.start(); o.wait_ready(3)
    assert (real / "ok.json") not in o._known   # stat failed -> excluded
    o.stop()
