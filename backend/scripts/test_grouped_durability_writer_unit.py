#!/usr/bin/env python3
"""Dedicated unit coverage for backend/grouped_durability_writer.py.

grouped_durability_writer.py is the durable session-state writer: it batches
replace/unlink intents onto a background thread and commits them atomically
(tempfile -> fsync -> os.replace), with size/age-bounded batching, crash-phase
hooks, generation receipts, and timeout-aware drain/close. The only existing
owner, test_grouped_durability_writer.py, is a standalone __main__ script
(pytest collects 0 items), so the module was effectively pytest-ownerless at
the unit tier (~18% import-time only).

This file drives every callable + branch hermetically against an isolated
BETTER_AGENT_HOME tempdir. Real threads and a real filesystem back the module
(it IS a threading+fs component); collaborators are exercised directly. No
real state is ever touched.
"""
from __future__ import annotations

import sys
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path

import pytest

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-gdw-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(str(_TEST_HOME))

import grouped_durability_writer as gdw  # noqa: E402


# --- helpers -----------------------------------------------------------------


def _raise_oserror(*_args, **_kwargs) -> int:
    raise OSError("boom")


def _install_dir_open_boom(monkeypatch):
    """Patch os.open to raise OSError once the returned armer is called.

    Real os.open is served until armed, so the earlier mkstemp phase still
    works; only the dir-fsync phase (after the hook arms it) fails. Used to
    drive both the posix (re-raise) and Windows (swallow) dir-fsync branches.
    """
    real_open = gdw.os.open
    armed = {"v": False}

    def gated_open(*args, **kwargs):
        if armed["v"]:
            raise OSError("dir open boom")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(gdw.os, "open", gated_open)
    return lambda: armed.__setitem__("v", True)


def _make_stalled_writer():
    """A writer whose worker blocks at the ``after_temp_flush`` phase until
    ``proceed`` is set. ``engaged`` signals the worker has reached the stall
    (so a test can assert mid-commit state without sleeps)."""
    proceed = threading.Event()
    engaged = threading.Event()

    def hook(phase: str, _snapshot) -> None:
        if phase == "after_temp_flush":
            engaged.set()
            proceed.wait(30.0)

    writer = gdw.GroupedDurabilityWriter(max_batch_age_s=0.0, crash_hook=hook)
    return writer, proceed, engaged


# --- construction ------------------------------------------------------------


def test_init_validates_batch_size_and_age():
    with pytest.raises(ValueError):
        gdw.GroupedDurabilityWriter(max_batch_size=0)
    with pytest.raises(ValueError):
        gdw.GroupedDurabilityWriter(max_batch_age_s=-0.001)
    # valid boundaries: size 1, age 0
    w = gdw.GroupedDurabilityWriter(max_batch_size=1, max_batch_age_s=0.0)
    try:
        assert w.pending_count() == 0
    finally:
        w.close()


def test_init_metric_prefix_uses_thread_name():
    w = gdw.GroupedDurabilityWriter(thread_name="custom-name")
    try:
        assert w._metric_prefix == "durability_writer.custom-name"
    finally:
        w.close()


# --- happy paths -------------------------------------------------------------


def test_replace_writes_file_and_returns_generation(tmp_path):
    w = gdw.GroupedDurabilityWriter()
    try:
        target = tmp_path / "nested" / "f.bin"
        receipt = w.replace(target, b"payload")
        assert receipt.wait(5.0) >= 1
        w.drain()  # deadline=None branch
        assert target.read_bytes() == b"payload"
        assert w.pending_count() == 0
    finally:
        w.close()


def test_replace_rejects_non_bytes(tmp_path):
    w = gdw.GroupedDurabilityWriter()
    try:
        with pytest.raises(TypeError):
            w.replace(tmp_path / "f", "not-bytes")  # type: ignore[arg-type]
    finally:
        w.close()


def test_unlink_removes_file(tmp_path):
    w = gdw.GroupedDurabilityWriter()
    try:
        target = tmp_path / "f"
        target.write_bytes(b"here")
        receipt = w.unlink(target)
        assert receipt.wait(5.0) >= 1
        w.drain(5.0)
        assert not target.exists()
    finally:
        w.close()


def test_batch_coalesces_into_shared_high_water(tmp_path):
    w = gdw.GroupedDurabilityWriter(max_batch_size=64, max_batch_age_s=1.0)
    try:
        targets = [tmp_path / f"f{i}" for i in range(3)]
        receipts = [w.replace(t, bytes([i])) for i, t in enumerate(targets)]
        high_water = receipts[0].wait(5.0)
        for r in receipts:
            assert r.wait(5.0) == high_water  # one batch, shared high-water mark
        w.drain(5.0)
        for i, t in enumerate(targets):
            assert t.read_bytes() == bytes([i])
    finally:
        w.close()


def test_max_batch_size_one_commits_individually(tmp_path):
    w = gdw.GroupedDurabilityWriter(max_batch_size=1)
    try:
        r1 = w.replace(tmp_path / "a", b"1")
        r2 = w.replace(tmp_path / "b", b"2")
        g1 = r1.wait(5.0)
        g2 = r2.wait(5.0)
        assert g1 < g2  # two separate batches
        w.drain(5.0)
    finally:
        w.close()


def test_crash_hook_observes_all_phases_and_snapshot(tmp_path):
    phases: list[str] = []
    captured: dict[str, gdw.BatchSnapshot] = {}

    def hook(phase: str, snapshot: gdw.BatchSnapshot) -> None:
        phases.append(phase)
        if phase == "after_temp_flush":
            captured["snap"] = snapshot

    w = gdw.GroupedDurabilityWriter(
        max_batch_size=64, max_batch_age_s=1.0, crash_hook=hook
    )
    try:
        t1 = tmp_path / "a" / "f1"
        t2 = tmp_path / "b" / "f2"
        r1 = w.replace(t1, b"1")
        r2 = w.replace(t2, b"2")
        r1.wait(5.0)
        r2.wait(5.0)
        w.drain(5.0)

        assert phases == [
            "after_temp_flush",
            "after_file_fsync",
            "after_mutation",
            "after_dir_fsync",
            "before_ack",
        ]
        snap = captured["snap"]
        assert isinstance(snap, gdw.BatchSnapshot)
        assert snap.parent_dirs == (t1.parent, t2.parent)
        assert set(snap.targets) == {t1, t2}
        assert len(snap.generations) == 2
    finally:
        w.close()


# --- drain / wait / close timeout branches -----------------------------------


def test_drain_times_out_while_committing(tmp_path):
    w, proceed, engaged = _make_stalled_writer()
    try:
        w.replace(tmp_path / "f", b"x")
        assert engaged.wait(5.0)
        with pytest.raises(TimeoutError):
            w.drain(timeout=0.02)
    finally:
        proceed.set()
        w.close()


def test_receipt_wait_times_out_while_committing(tmp_path):
    w, proceed, engaged = _make_stalled_writer()
    try:
        receipt = w.replace(tmp_path / "f", b"x")
        assert engaged.wait(5.0)
        with pytest.raises(TimeoutError):
            receipt.wait(timeout=0.02)
    finally:
        proceed.set()
        w.close()


def test_pending_count_tracks_active_batch(tmp_path):
    w, proceed, engaged = _make_stalled_writer()
    try:
        w.replace(tmp_path / "f", b"x")
        assert engaged.wait(5.0)
        assert w.pending_count() == 1
        proceed.set()
        w.drain(5.0)
        assert w.pending_count() == 0
    finally:
        proceed.set()
        w.close()


def test_close_is_idempotent():
    w = gdw.GroupedDurabilityWriter()
    w.close()
    w.close()  # already-closed early return


def test_close_times_out_while_committing(tmp_path):
    w, proceed, engaged = _make_stalled_writer()
    try:
        w.replace(tmp_path / "f", b"x")
        assert engaged.wait(5.0)
        with pytest.raises(TimeoutError):
            w.close(timeout=0.02)
    finally:
        proceed.set()
        w.close()  # unstalled -> clean shutdown


def test_enqueue_after_close_raises(tmp_path):
    w = gdw.GroupedDurabilityWriter()
    w.close()
    with pytest.raises(RuntimeError):
        w.replace(tmp_path / "f", b"x")


# --- commit failure paths ----------------------------------------------------


def test_commit_failure_sets_exception_on_futures(tmp_path):
    def hook(phase: str, _snapshot) -> None:
        if phase == "after_temp_flush":
            raise RuntimeError("commit boom")

    w = gdw.GroupedDurabilityWriter(max_batch_age_s=0.0, crash_hook=hook)
    try:
        receipt = w.replace(tmp_path / "d" / "f", b"x")
        with pytest.raises(RuntimeError, match="commit boom"):
            receipt.wait()
    finally:
        w.close()


def test_commit_skips_already_done_future(tmp_path):
    committed = threading.Event()
    done_future: Future = Future()
    done_future.set_result(999)

    def hook(phase: str, _snapshot) -> None:
        if phase == "after_temp_flush":
            committed.set()
            raise RuntimeError("boom")

    w = gdw.GroupedDurabilityWriter(max_batch_age_s=0.0, crash_hook=hook)
    try:
        with w._cv:
            w._pending.append(gdw._Intent(7, tmp_path / "x", b"data", done_future))
            w._cv.notify_all()
        assert committed.wait(5.0)
        # already-resolved future is skipped by the failure fan-out
        assert done_future.result() == 999
    finally:
        w.close()


def test_temp_write_failure_cleans_and_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(gdw.os, "fdopen", _raise_oserror)
    w = gdw.GroupedDurabilityWriter(max_batch_age_s=0.0)
    try:
        parent = tmp_path / "d"
        parent.mkdir()
        receipt = w.replace(parent / "f", b"x")
        with pytest.raises(OSError):
            receipt.wait()
        # the staged temp file is cleaned up despite the failure
        assert list(parent.glob("*.durability.tmp")) == []
    finally:
        w.close()


def test_dir_fsync_failure_propagates_on_posix(tmp_path, monkeypatch):
    arm = _install_dir_open_boom(monkeypatch)

    def hook(phase: str, _snapshot) -> None:
        if phase == "after_mutation":
            arm()

    w = gdw.GroupedDurabilityWriter(max_batch_age_s=0.0, crash_hook=hook)
    try:
        receipt = w.replace(tmp_path / "d" / "f", b"x")
        with pytest.raises(OSError):
            receipt.wait()
    finally:
        w.close()


def test_dir_fsync_failure_swallowed_on_windows(tmp_path, monkeypatch):
    arm = _install_dir_open_boom(monkeypatch)

    def hook(phase: str, _snapshot) -> None:
        if phase == "after_mutation":
            monkeypatch.setattr(gdw.os, "name", "nt")
            arm()

    w = gdw.GroupedDurabilityWriter(max_batch_age_s=0.0, crash_hook=hook)
    try:
        target = tmp_path / "d" / "f"
        receipt = w.replace(target, b"x")
        assert receipt.wait(5.0) >= 1  # dir-fsync failure swallowed on Windows
        assert target.read_bytes() == b"x"
        w.drain(5.0)
    finally:
        w.close()
