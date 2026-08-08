#!/usr/bin/env python3
"""Failing-first coverage for Package E (ADR 0009 runs/diagnostics gaps):
E1 turn_id, E2 heartbeat/stalled, E3 richer RunPhase, E4 real cursor
pagination, E5 authoritative run<->turn linkage, E7 richer RunDetail
entries. E6 (usage analytics) has its own dedicated suite —
`backend/scripts/test_usage_analytics.py` — plus one integration check
in `test_surface_adapters.py`.

Every test here failed before this pass: `RunRecord` carried no
`turn_id`/`last_heartbeat_at` fields at all (AttributeError), `_phase()`
only ever returned COMPLETED/FAILED/RUNNING, `list_runs()` returned a bare
tuple with no `next_cursor` (`RunPage` didn't exist), and
`_on_lifecycle()` picked the same-session-most-recent-started run instead
of matching the event's own turn.

A later pass (`run.state.*` event-driven projection, see
`turn_manager.py`'s `_publish_run_state_fact` and this adapter's
`_on_run_state_fact`) added the "run-state facts" section below: every
test there failed before that pass too — `RunSummary`/`RunDetail` had no
`kind`/`target_message_id`/`delegation_id`/`pid`/`stalled_at`/
`recovered_at_startup` fields, `_phase()` never returned RECONNECTING or
UNKNOWN_AFTER_RECOVERY, and the adapter had no `run.state.*` bus binding
at all — `_on_run_state_fact` did not exist.

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched) — same idiom as test_surface_adapters.py /
test_store_access.py.

Run:
    PYTHONPATH=.:./backend:./sdk python3 -m pytest backend/scripts/test_adapter_runs.py -q
    PYTHONPATH=.:./backend:./sdk python3 backend/scripts/test_adapter_runs.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-runs-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import runs_dir  # noqa: E402

from backend.adapters.runs_adapter import RunsSurfaceAdapter  # noqa: E402
from backend.adapters.store_access import store_access  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.identity import Ok, StaleCursor  # noqa: E402
from backend.surface_contract.runs_surface import (  # noqa: E402
    DetailValueKind,
    RunPhase,
    RunSummaryUpsert,
)


def _publish_run_state_fact(fact_type: str, session_id: str, run_id: str, **payload) -> None:
    """Constructs+publishes a `run.state.*` BusEvent the same shape
    `turn_manager._publish_run_state_fact` produces, via the directly
    `await`-able `bus.publish` (not `publish_threadsafe` — these tests run
    on one thread/loop, matching the existing E5 tests' own style)."""
    asyncio.run(
        bus.publish(BusEvent(
            type=fact_type, root_id=session_id, sid=session_id,
            payload={"run_id": run_id, "session_id": session_id, **payload},
            run_id=run_id, persist=False,
        ))
    )


def _seed_run(
    app_session_id: str,
    *,
    success: bool | None = None,
    error: str | None = None,
    turn_id: str | None = None,
    turn_ids: tuple[str, ...] | None = None,
    heartbeat_age_s: float | None = None,
    started_offset_s: float = 0.0,
) -> str:
    """Seeds one `runs/<run_id>/` dir the same way the real runner does:
    `state.json` (backend discovery), an optional `turns/<turn_id>/`
    child (or several, via `turn_ids`, to exercise the multi-turn-dir
    tie-break), an optional `runner_alive` heartbeat sentinel backdated by
    `heartbeat_age_s` seconds, and an optional `complete.json`."""
    root = runs_dir.runs_root()
    run_id = f"run-{uuid.uuid4().hex}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.atomic_write_json(run_dir / "state.json", {
        "session_id": f"prov-{run_id}",
        "jsonl_path": str(run_dir / "stream.jsonl"),
        "app_session_id": app_session_id,
    })
    for tid in (turn_ids if turn_ids is not None else ((turn_id,) if turn_id else ())):
        turn_dir = run_dir / "turns" / tid
        turn_dir.mkdir(parents=True, exist_ok=True)
        # Stagger mtimes deterministically so "latest turn dir wins" is
        # unambiguous under the tie-break test below.
        os.utime(turn_dir, None)
        time.sleep(0.01)
    if heartbeat_age_s is not None:
        alive_path = runs_dir.runner_alive_path(run_dir)
        alive_path.write_text("", encoding="utf-8")
        stamp = time.time() - heartbeat_age_s
        os.utime(alive_path, (stamp, stamp))
    if started_offset_s:
        stamp = time.time() - started_offset_s
        os.utime(run_dir, (stamp, stamp))
    if success is not None:
        runs_dir.atomic_write_json(run_dir / "complete.json", {"success": success, "error": error})
    return run_id


# ---------------------------------------------------------------------------
# E1 — turn_id sourced from `turns/<turn_id>` (no heuristic)
# ---------------------------------------------------------------------------

def test_turn_id_absent_before_turn_directory_exists() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)
    record = next(r for r in store_access.list_run_records() if r.run_id == run_id)
    assert record.turn_id is None


def test_turn_id_sourced_from_single_turn_directory() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, turn_id=turn_id)
    record = next(r for r in store_access.list_run_records() if r.run_id == run_id)
    assert record.turn_id == turn_id

    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert result.value.runs[0].turn_id == turn_id


def test_turn_id_picks_most_recently_modified_when_multiple_exist() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    older, newer = f"turn-{uuid.uuid4().hex}", f"turn-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, turn_ids=(older, newer))
    record = next(r for r in store_access.list_run_records() if r.run_id == run_id)
    assert record.turn_id == newer


# ---------------------------------------------------------------------------
# E2 — last_heartbeat_at from `runner_alive` mtime, STALLED phase
# ---------------------------------------------------------------------------

def test_last_heartbeat_at_absent_without_runner_alive_sentinel() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)
    record = next(r for r in store_access.list_run_records() if r.run_id == run_id)
    assert record.last_heartbeat_at is None


def test_last_heartbeat_at_reflects_runner_alive_mtime() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, heartbeat_age_s=2.0)
    record = next(r for r in store_access.list_run_records() if r.run_id == run_id)
    assert record.last_heartbeat_at is not None
    assert abs(time.time() - record.last_heartbeat_at - 2.0) < 1.0


def test_phase_starting_when_no_heartbeat_yet() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    _seed_run(session_id)
    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert result.value.runs[0].phase == RunPhase.STARTING


def test_phase_running_when_heartbeat_fresh() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    _seed_run(session_id, heartbeat_age_s=1.0)
    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert result.value.runs[0].phase == RunPhase.RUNNING


def test_phase_stalled_when_heartbeat_beyond_threshold() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    _seed_run(session_id, heartbeat_age_s=45.0)  # > runs_adapter._STALE_HEARTBEAT_S (30s)
    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert result.value.runs[0].phase == RunPhase.STALLED


def test_stalled_never_reported_for_a_terminal_run() -> None:
    """A stale heartbeat on an already-COMPLETED run must not read STALLED
    — success/failure short-circuits phase before heartbeat is consulted."""
    session_id = f"app-{uuid.uuid4().hex}"
    _seed_run(session_id, success=True, heartbeat_age_s=999.0)
    adapter = RunsSurfaceAdapter()
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert result.value.runs[0].phase == RunPhase.COMPLETED


# ---------------------------------------------------------------------------
# E4 — real cursor pagination
# ---------------------------------------------------------------------------

def test_list_runs_paginates_without_overlap_or_gap() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    seeded = {_seed_run(session_id, success=True) for _ in range(55)}

    adapter = RunsSurfaceAdapter()
    first = adapter.list_runs(session_id, None)
    assert isinstance(first, Ok), first
    assert len(first.value.runs) == 50
    assert first.value.next_cursor is not None

    second = adapter.list_runs(session_id, first.value.next_cursor)
    assert isinstance(second, Ok), second
    assert len(second.value.runs) == 5
    assert second.value.next_cursor is None

    seen = {r.run_id for r in first.value.runs} | {r.run_id for r in second.value.runs}
    assert seen == seeded, (seeded - seen, seen - seeded)
    # no overlap between pages
    assert not ({r.run_id for r in first.value.runs} & {r.run_id for r in second.value.runs})


def test_list_runs_stale_cursor_across_adapter_incarnations() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    for _ in range(55):
        _seed_run(session_id, success=True)

    first_adapter = RunsSurfaceAdapter()
    first = first_adapter.list_runs(session_id, None)
    assert isinstance(first, Ok), first
    assert first.value.next_cursor is not None

    # A fresh adapter instance has a fresh (random) projection incarnation
    # — the old cursor must be rejected, not silently reinterpreted.
    second_adapter = RunsSurfaceAdapter()
    stale = second_adapter.list_runs(session_id, first.value.next_cursor)
    assert isinstance(stale, StaleCursor), stale


# ---------------------------------------------------------------------------
# E5 — authoritative run<->turn linkage (no same-session-most-recent guess)
# ---------------------------------------------------------------------------

def test_lifecycle_fact_matches_run_by_exact_turn_id_not_most_recent() -> None:
    """Two runs on the SAME session; the OLDER one owns the turn_id the
    lifecycle fact names. The pre-fix heuristic (most-recently-started run
    for the session) would have misattributed this to the newer run."""
    session_id = f"app-{uuid.uuid4().hex}"
    turn_a, turn_b = f"turn-{uuid.uuid4().hex}", f"turn-{uuid.uuid4().hex}"
    run_a = _seed_run(session_id, turn_id=turn_a, started_offset_s=10.0)
    run_b = _seed_run(session_id, turn_id=turn_b, started_offset_s=0.0)
    assert run_a != run_b

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(
            bus.publish(
                BusEvent(
                    type="lifecycle.turn_complete", root_id=session_id, sid=session_id,
                    payload={"reason": "success", "execution_turn_id": turn_a}, persist=False,
                )
            )
        )
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert upserts, received
        assert upserts[0].summary.run_id == run_a
        assert upserts[0].summary.turn_id == turn_a
    finally:
        sub.close()


def test_lifecycle_fact_does_not_scan_every_session_in_the_backend() -> None:
    """Root-cause regression for the 2026-08-08 zero-runners-spawning
    incident: `_on_lifecycle` used to call `store_access.list_run_records()`,
    which walks `runs_dir.run_dirs_by_app_session` — an O(every session
    that has ever run) discovery scan — to resolve ONE record for the ONE
    session the event already names. With tens of thousands of run dirs
    accumulated (the real ledger had ~78k rows), that per-turn-start scan,
    globally serialized behind `_scan_broadcast_lock`, could take minutes
    under host CPU starvation — every session's turn dispatch queued
    behind the same lock, appearing to hang forever with no further log
    output past "turn_start". Seeds several OTHER sessions' runs plus the
    target session's, then asserts `_on_lifecycle` never calls
    `run_dirs_by_app_session` at all (only the session-scoped
    `cached_run_dirs_for_app_session` path may run) while still resolving
    and broadcasting the correct record."""
    for _ in range(5):
        _seed_run(f"app-{uuid.uuid4().hex}", turn_id=f"turn-{uuid.uuid4().hex}")
    session_id = f"app-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, turn_id=turn_id)

    original = runs_dir.run_dirs_by_app_session
    calls: list = []

    def _tracking(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    runs_dir.run_dirs_by_app_session = _tracking
    try:
        adapter = RunsSurfaceAdapter()
        adapter.bind()
        received: list = []
        sub = adapter.subscribe(received.append)
        try:
            asyncio.run(
                bus.publish(
                    BusEvent(
                        type="lifecycle.turn_start", root_id=session_id, sid=session_id,
                        payload={"execution_turn_id": turn_id}, persist=False,
                    )
                )
            )
        finally:
            sub.close()
    finally:
        runs_dir.run_dirs_by_app_session = original

    assert calls == [], (
        f"_on_lifecycle triggered {len(calls)} whole-backend discovery "
        "scan(s) instead of a session-scoped lookup"
    )
    upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
    assert upserts, received
    assert upserts[0].summary.run_id == run_id
    assert upserts[0].summary.turn_id == turn_id


def test_run_state_fact_does_not_scan_every_session_in_the_backend() -> None:
    """Same root-cause regression as
    `test_lifecycle_fact_does_not_scan_every_session_in_the_backend`, for
    `_on_run_state_fact` (the `run.state.*` sibling handler, same
    `_scan_broadcast_lock` bottleneck)."""
    for _ in range(5):
        _seed_run(f"app-{uuid.uuid4().hex}", turn_id=f"turn-{uuid.uuid4().hex}")
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    original = runs_dir.run_dirs_by_app_session
    calls: list = []

    def _tracking(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    runs_dir.run_dirs_by_app_session = _tracking
    try:
        adapter = RunsSurfaceAdapter()
        adapter.bind()
        received: list = []
        sub = adapter.subscribe(received.append)
        try:
            _publish_run_state_fact("run.state.pid_changed", session_id, run_id, pid=4242)
        finally:
            sub.close()
    finally:
        runs_dir.run_dirs_by_app_session = original

    assert calls == [], (
        f"_on_run_state_fact triggered {len(calls)} whole-backend discovery "
        "scan(s) instead of a session-scoped lookup"
    )
    upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
    assert upserts, received
    assert upserts[0].summary.run_id == run_id
    assert upserts[0].summary.pid == 4242


def test_lifecycle_fact_without_execution_turn_id_does_not_broadcast() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    _seed_run(session_id, turn_id=f"turn-{uuid.uuid4().hex}")

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        asyncio.run(
            bus.publish(
                BusEvent(
                    type="lifecycle.turn_start", root_id=session_id, sid=session_id,
                    payload={}, persist=False,
                )
            )
        )
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert not upserts, received
    finally:
        sub.close()


# ---------------------------------------------------------------------------
# E7 — richer RunDetail entries
# ---------------------------------------------------------------------------

def test_run_detail_includes_turn_id_and_heartbeat_entries() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, turn_id=turn_id, heartbeat_age_s=5.0)

    adapter = RunsSurfaceAdapter()
    result = adapter.run_detail(run_id)
    assert isinstance(result, Ok), result
    entries = {e.key: e.value for e in result.value.entries}
    assert entries["run.detail.turn_id"] == turn_id
    assert "run.detail.last_heartbeat_at" in entries
    assert entries["run.detail.heartbeat_silence_seconds"] >= 0


def test_run_detail_omits_turn_id_and_heartbeat_entries_when_absent() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, success=True)

    adapter = RunsSurfaceAdapter()
    result = adapter.run_detail(run_id)
    assert isinstance(result, Ok), result
    keys = {e.key for e in result.value.entries}
    assert "run.detail.turn_id" not in keys
    assert "run.detail.last_heartbeat_at" not in keys
    assert "run.detail.heartbeat_silence_seconds" not in keys


# ---------------------------------------------------------------------------
# `run.state.*` facts — event-driven live overlay (turn_manager.py's
# `_run_state` mutation facts, folded by `_on_run_state_fact`)
# ---------------------------------------------------------------------------

def test_run_state_pid_changed_fact_updates_summary_pid_and_broadcasts() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        _publish_run_state_fact("run.state.pid_changed", session_id, run_id, pid=4321)
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert upserts, received
        assert upserts[-1].summary.pid == 4321

        result = adapter.list_runs(session_id, None)
        assert isinstance(result, Ok), result
        assert result.value.runs[0].pid == 4321
    finally:
        sub.close()


def test_run_state_anchor_changed_fact_updates_kind_target_delegation() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    _publish_run_state_fact(
        "run.state.anchor_changed", session_id, run_id,
        kind="worker", target_message_id="msg-1", delegation_id="deleg-1", pid=None,
    )
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    summary = result.value.runs[0]
    assert summary.kind is not None and summary.kind.value == "worker"
    assert summary.target_message_id == "msg-1"
    assert summary.delegation_id == "deleg-1"


def test_run_state_startup_phase_changed_populates_startup_progress() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    _publish_run_state_fact(
        "run.state.startup_phase_changed", session_id, run_id,
        startup_phase="awaiting_provider_start", stalled_at=None,
    )
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    summary = result.value.runs[0]
    assert summary.startup is not None
    assert summary.startup.phase == "awaiting_provider_start"
    assert summary.startup.progress is None


def test_run_state_startup_phase_stalled_overrides_fresh_heartbeat() -> None:
    """A fresh `runner_alive` heartbeat alone would read RUNNING (E2/E3) —
    turn_manager's own "stalled" classification must win over that."""
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, heartbeat_age_s=1.0)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    stalled_at_iso = "2020-01-01T00:00:00+00:00"
    _publish_run_state_fact(
        "run.state.startup_phase_changed", session_id, run_id,
        startup_phase="stalled", stalled_at=stalled_at_iso,
    )
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    summary = result.value.runs[0]
    assert summary.phase == RunPhase.STALLED
    assert summary.stalled_at is not None and summary.stalled_at > 0


def test_run_state_recovery_classified_without_heartbeat_is_unknown_after_recovery() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)  # no heartbeat sentinel seeded

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    _publish_run_state_fact(
        "run.state.recovery_classified", session_id, run_id,
        classification="recovered_at_startup",
    )
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    summary = result.value.runs[0]
    assert summary.recovered_at_startup is True
    assert summary.phase == RunPhase.UNKNOWN_AFTER_RECOVERY


def test_run_state_recovery_classified_with_fresh_heartbeat_is_reconnecting() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id, heartbeat_age_s=1.0)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    _publish_run_state_fact(
        "run.state.recovery_classified", session_id, run_id,
        classification="recovered_at_startup",
    )
    result = adapter.list_runs(session_id, None)
    assert isinstance(result, Ok), result
    assert result.value.runs[0].phase == RunPhase.RECONNECTING


def test_run_state_removed_fact_clears_live_overlay() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    _publish_run_state_fact("run.state.pid_changed", session_id, run_id, pid=123)
    mid = adapter.list_runs(session_id, None)
    assert isinstance(mid, Ok) and mid.value.runs[0].pid == 123

    _publish_run_state_fact("run.state.removed", session_id, run_id, reason="explicit")
    after = adapter.list_runs(session_id, None)
    assert isinstance(after, Ok), after
    assert after.value.runs[0].pid is None


def test_run_detail_includes_live_overlay_entries() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    _publish_run_state_fact(
        "run.state.anchor_changed", session_id, run_id,
        kind="manager", target_message_id="msg-x", delegation_id="deleg-x", pid=None,
    )
    _publish_run_state_fact("run.state.pid_changed", session_id, run_id, pid=555)

    result = adapter.run_detail(run_id)
    assert isinstance(result, Ok), result
    entries = {e.key: e.value for e in result.value.entries}
    assert entries["run.detail.kind"] == "manager"
    assert entries["run.detail.target_message_id"] == "msg-x"
    assert entries["run.detail.delegation_id"] == "deleg-x"
    assert entries["run.detail.pid"] == 555


def test_run_detail_process_tree_entry_present_only_when_pid_known() -> None:
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = _seed_run(session_id)

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    no_pid = adapter.run_detail(run_id)
    assert isinstance(no_pid, Ok), no_pid
    assert "run.detail.process_tree" not in {e.key for e in no_pid.value.entries}

    _publish_run_state_fact("run.state.pid_changed", session_id, run_id, pid=os.getpid())
    with_pid = adapter.run_detail(run_id)
    assert isinstance(with_pid, Ok), with_pid
    tree_entries = [e for e in with_pid.value.entries if e.key == "run.detail.process_tree"]
    assert len(tree_entries) == 1, with_pid.value.entries
    assert tree_entries[0].value_kind == DetailValueKind.PROCESS_TREE
    assert isinstance(tree_entries[0].value, tuple)


def test_run_state_fact_before_run_record_exists_updates_overlay_without_broadcast() -> None:
    """A fact can arrive before the run's directory is on disk (admission
    race, same rationale as `_on_lifecycle`'s fail-closed branch) — the
    overlay must still update so a later pull is correct, even though no
    RunSummaryUpsert can be built (no RunRecord to build it from) yet."""
    session_id = f"app-{uuid.uuid4().hex}"
    run_id = f"run-{uuid.uuid4().hex}"  # never seeded on disk

    adapter = RunsSurfaceAdapter()
    adapter.bind()
    received: list = []
    sub = adapter.subscribe(received.append)
    try:
        _publish_run_state_fact("run.state.pid_changed", session_id, run_id, pid=99)
        upserts = [f for f in received if isinstance(f, RunSummaryUpsert)]
        assert not upserts, received
        assert adapter._live[run_id].pid == 99
    finally:
        sub.close()


_TESTS = [
    test_turn_id_absent_before_turn_directory_exists,
    test_turn_id_sourced_from_single_turn_directory,
    test_turn_id_picks_most_recently_modified_when_multiple_exist,
    test_last_heartbeat_at_absent_without_runner_alive_sentinel,
    test_last_heartbeat_at_reflects_runner_alive_mtime,
    test_phase_starting_when_no_heartbeat_yet,
    test_phase_running_when_heartbeat_fresh,
    test_phase_stalled_when_heartbeat_beyond_threshold,
    test_stalled_never_reported_for_a_terminal_run,
    test_list_runs_paginates_without_overlap_or_gap,
    test_list_runs_stale_cursor_across_adapter_incarnations,
    test_lifecycle_fact_matches_run_by_exact_turn_id_not_most_recent,
    test_lifecycle_fact_does_not_scan_every_session_in_the_backend,
    test_run_state_fact_does_not_scan_every_session_in_the_backend,
    test_lifecycle_fact_without_execution_turn_id_does_not_broadcast,
    test_run_detail_includes_turn_id_and_heartbeat_entries,
    test_run_detail_omits_turn_id_and_heartbeat_entries_when_absent,
    test_run_state_pid_changed_fact_updates_summary_pid_and_broadcasts,
    test_run_state_anchor_changed_fact_updates_kind_target_delegation,
    test_run_state_startup_phase_changed_populates_startup_progress,
    test_run_state_startup_phase_stalled_overrides_fresh_heartbeat,
    test_run_state_recovery_classified_without_heartbeat_is_unknown_after_recovery,
    test_run_state_recovery_classified_with_fresh_heartbeat_is_reconnecting,
    test_run_state_removed_fact_clears_live_overlay,
    test_run_detail_includes_live_overlay_entries,
    test_run_detail_process_tree_entry_present_only_when_pid_known,
    test_run_state_fact_before_run_record_exists_updates_overlay_without_broadcast,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"ok      {fn.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL    {fn.__name__}")
            import traceback
            traceback.print_exc()
        except Exception:
            failures += 1
            print(f"ERROR   {fn.__name__}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
