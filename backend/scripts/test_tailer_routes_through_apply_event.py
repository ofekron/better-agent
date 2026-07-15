"""Regression test for `OwnedClaudeJsonlTailer._dispatch` consolidation.

Locks the contract that the **primary agent's** CLI session jsonl tailer
funnels through `OrchestrationStrategy.apply_event` / `ingest_orphan`,
while **worker-fork** tailers write fork-identity backup rows
(`sid=fork agent_sid`, `msg_id=None`, `source=FORK_BACKUP_SOURCE`).

Three subtests:

  A. Primary agent + streaming msg present → `_dispatch` appends the
     event to flat `msg.events` (render-tree mutation). Pre-refactor
     this does NOT happen — the tailer wrote events.jsonl only.

  B. Primary agent + NO streaming msg (latest assistant msg finalized)
     → `_dispatch` ingests with `msg_id=None`, arms the
     reconcile-dirty flag, and does NOT mutate the render tree.

  C. Worker-fork tailer (agent_sid ∉ session's primary agent sids) →
     `_dispatch` writes a fork-identity backup row (sid=fork
     agent_sid, msg_id=None, source=FORK_BACKUP_SOURCE); parent's
     flat `msg.events` is untouched. This regression-locks the gate
     that prevents worker raw lines from polluting the parent
     manager's events list.

Run with:
    cd backend && .venv/bin/python scripts/test_tailer_routes_through_apply_event.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

# State-isolation rule: set BETTER_CLAUDE_HOME BEFORE importing any
# backend module so every store, runs root, traces dir lands in a
# throwaway tempdir.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-tailer-")

from pathlib import Path

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

from event_ingester import event_ingester  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
from event_journal import FORK_BACKUP_SOURCE, event_journal_writer  # noqa: E402
from jsonl_tailer import OwnedClaudeJsonlTailer  # noqa: E402
import native_files_manager as nfm  # noqa: E402
from native_files_manager import native_files  # noqa: E402
from paths import ba_home  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── helpers ──────────────────────────────────────────────────────

def _mk_session_with_streaming_msg(
    *, primary_agent_sid: str,
) -> tuple[str, str, str]:
    """Create a manager-mode session whose primary `agent_session_id`
    is `primary_agent_sid`, append a streaming assistant message, and
    return `(sid, root_id, msg_id)`."""
    from orchs import get_strategy
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    session_manager.set_agent_sid(sid, "manager", primary_agent_sid)
    scaffold = get_strategy("manager").build_assistant_scaffold()
    scaffold["isStreaming"] = True
    session_manager.append_assistant_msg(sid, scaffold)
    root_id = session_manager._root_id_for(sid)
    return sid, root_id, scaffold["id"]


def _enriched(uuid: str, text: str) -> dict:
    """Mimic an enriched claude jsonl line as `ClaudeJsonlTailer` would
    hand it to `_dispatch`. Minimal shape: agent_message-data with a
    uuid so `apply_event` can dedup."""
    return {
        "uuid": uuid,
        "type": "assistant",
        "message": {"content": text},
    }


def _msg_events(sid: str, msg_id: str) -> list:
    """Read the live render-tree's flat msg.events for the given msg."""
    sess = session_manager.get(sid) or {}
    for m in sess.get("messages") or []:
        if m.get("id") == msg_id:
            return (m.get("events")) or []
    return []


def _events_jsonl_for(root_id: str, sid: str) -> list[dict]:
    raw, _, _ = event_ingester.read_events(
        root_id, limit=10_000, sid_filter=sid,
    )
    return raw


def _reset_native_files() -> None:
    for task in native_files._primary_resolution_tasks.values():
        task.cancel()
    native_files._primary_resolution_tasks.clear()
    native_files._targets.clear()
    native_files._demand.clear()
    native_files._tailers.clear()
    native_files._seeded.clear()
    native_files._seed_locks.clear()
    native_files._native_path_locks.clear()


async def _drain_primary_resolution_tasks(
    *expected: tuple[str, str],
) -> None:
    for owning, agent_sid in expected:
        key = (owning, agent_sid)
        if key not in native_files._primary_resolution_tasks:
            if agent_sid not in native_files._targets.get(owning, {}):
                raise AssertionError(f"missing primary resolution task for {key!r}")
    while native_files._primary_resolution_tasks:
        tasks = list(native_files._primary_resolution_tasks.values())
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)


# ─── subtests ─────────────────────────────────────────────────────

async def test_a_primary_streaming_orphan_ingest_backup() -> bool:
    """Primary agent tailer with a streaming msg present ALSO uses
    `ingest_orphan` (per `jsonl_tailer.py:741-753`). The render tree
    is owned by the orchestrator's `save_ws_callback → apply_event`
    path; the tailer is a crash-window backup that only writes to
    events.jsonl. Grafting on the streaming msg from the tailer was
    deliberately removed because it would graft stale events from a
    previous turn — `apply_event`'s per-msg dedup only checks the
    target msg, so uuids already present in a prior msg pass through
    undetected.

    Contract today:
      - events.jsonl gets the row (durable for WS-tailer broadcast)
      - msg.events does NOT grow (apply_event is the render writer)
      - reconcile_dirty stays unset (streaming msg → not finalized →
        the orphan-event signal in event_ingester.ingest no-ops)
    """
    primary_sid = "agent-primary-A"
    sid, root_id, msg_id = _mk_session_with_streaming_msg(
        primary_agent_sid=primary_sid,
    )
    session_manager.consume_reconcile_dirty(root_id)

    before = len(_msg_events(sid, msg_id))

    tailer = OwnedClaudeJsonlTailer(
        root_id=root_id,
        app_session_id=sid,
        agent_sid=primary_sid,
        jsonl_path=Path("/tmp/unused.jsonl"),
        start_offset=0,
    )
    await tailer._dispatch(_enriched("uuid-A", "subtest-A-payload"))
    # ingest_orphan journals fire-and-forget onto the per-root shard
    # executor; drain it so the orphan row (and the no-op dirty check,
    # which runs on the shard thread inside ingest) are visible.
    event_journal_writer.barrier_sync(root_id)

    after = len(_msg_events(sid, msg_id))
    unchanged = after == before

    dirty = session_manager.consume_reconcile_dirty(root_id)

    rows = _events_jsonl_for(root_id, sid)
    orphan_row = next(
        (r for r in rows if (r.get("data") or {}).get("uuid") == "uuid-A"),
        None,
    )
    is_orphan = orphan_row is not None and orphan_row.get("msg_id") is None

    ok = unchanged and not dirty and is_orphan
    print(f"{PASS if ok else FAIL} A: primary+streaming → render unchanged "
          f"({unchanged}); reconcile_dirty={dirty} (must be False); "
          f"orphan-row={is_orphan}")
    return ok


async def test_b_primary_no_streaming_msg_orphan_path() -> bool:
    """Primary agent tailer with NO streaming msg → orphan ingest +
    reconcile-dirty armed + render tree unchanged."""
    primary_sid = "agent-primary-B"
    sid, root_id, msg_id = _mk_session_with_streaming_msg(
        primary_agent_sid=primary_sid,
    )
    # Finalize the assistant msg so it's no longer streaming.
    session_manager.set_streaming(sid, msg_id, False)
    # Clear any dirty flag left over from setup.
    session_manager.consume_reconcile_dirty(root_id)

    before = len(_msg_events(sid, msg_id))

    tailer = OwnedClaudeJsonlTailer(
        root_id=root_id,
        app_session_id=sid,
        agent_sid=primary_sid,
        jsonl_path=Path("/tmp/unused.jsonl"),
        start_offset=0,
    )
    await tailer._dispatch(_enriched("uuid-B", "subtest-B-payload"))
    # Drain the fire-and-forget journal write so the orphan row is on
    # disk and the shard thread's reconcile-dirty mark has fired before
    # the assertions read them.
    event_journal_writer.barrier_sync(root_id)

    after = len(_msg_events(sid, msg_id))
    unchanged = after == before

    dirty = session_manager.consume_reconcile_dirty(root_id)

    rows = _events_jsonl_for(root_id, sid)
    orphan_row = next(
        (r for r in rows if (r.get("data") or {}).get("uuid") == "uuid-B"),
        None,
    )
    is_orphan = orphan_row is not None and orphan_row.get("msg_id") is None

    ok = unchanged and dirty and is_orphan
    print(f"{PASS if ok else FAIL} B: primary+no-streaming → render unchanged "
          f"({unchanged}); reconcile_dirty={dirty}; orphan-row={is_orphan}")
    return ok


async def test_c_worker_fork_tailer_keeps_legacy_path() -> bool:
    """Worker-fork tailer (agent_sid ∉ session's primary agent sids)
    must NOT funnel through apply_event — it writes a fork-identity
    backup row: `sid=fork agent_sid` (NOT the parent app sid),
    `msg_id=None`, `source=FORK_BACKUP_SOURCE`. Parent's flat
    `msg.events` must NOT grow (no render-tree mutation), and the
    fork-identity stamping is what lets every ownership-resolution /
    hydrate / message-read path exclude these rows so they can never
    attach to a parent message.
    The tailer was constructed with `app_session_id=PARENT_app_session_id`,
    so routing through `apply_event(msg=parent_streaming_msg)` would
    graft worker raw SDK lines onto the parent's flat events list."""
    primary_sid = "agent-primary-C"
    sid, root_id, msg_id = _mk_session_with_streaming_msg(
        primary_agent_sid=primary_sid,
    )

    before = len(_msg_events(sid, msg_id))

    fork_sid = "agent-worker-fork-C"  # NOT the primary
    tailer = OwnedClaudeJsonlTailer(
        root_id=root_id,
        app_session_id=sid,  # ← PARENT's sid (mirrors orchestrator.py:813)
        agent_sid=fork_sid,
        jsonl_path=Path("/tmp/unused-fork.jsonl"),
        start_offset=0,
    )
    await tailer._dispatch(_enriched("uuid-C", "subtest-C-payload"))

    after = len(_msg_events(sid, msg_id))
    parent_unchanged = after == before

    # Fork backup rows carry the FORK's identity (sid=agent_sid), so
    # filter by the fork sid — a row under the parent sid would be the
    # old attribution bug.
    rows = _events_jsonl_for(root_id, fork_sid)
    fork_row = next(
        (r for r in rows if (r.get("data") or {}).get("uuid") == "uuid-C"),
        None,
    )
    is_fork_backup = (
        fork_row is not None
        and fork_row.get("source") == FORK_BACKUP_SOURCE
        and fork_row.get("msg_id") is None
    )

    ok = parent_unchanged and is_fork_backup
    print(f"{PASS if ok else FAIL} C: worker-fork → parent render unchanged "
          f"({parent_unchanged}); fork-identity row (sid=fork, msg_id=None, "
          f"source={FORK_BACKUP_SOURCE!r})={is_fork_backup}")
    return ok


async def test_d_native_paths_appends_discovered_targets() -> bool:
    _reset_native_files()

    sess = session_manager.create(
        name="native-paths", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    agent_sid = "codex-thread-D"
    run_dir = ba_home() / "runs" / "run-D"
    run_dir.mkdir(parents=True, exist_ok=True)
    rollout_path = "/tmp/rollout-native-paths-D.jsonl"
    (run_dir / "state.json").write_text(
        json.dumps({"session_id": agent_sid, "jsonl_path": rollout_path}),
        encoding="utf-8",
    )

    session_manager.set_agent_sid(sid, "native", agent_sid)
    await native_files._on_agent_sid(BusEvent(
        type="session.agent_sid_set",
        root_id=root_id,
        sid=sid,
        payload={"mode": "native", "agent_sid": agent_sid},
        persist=False,
        seq=7001,
    ))
    await native_files._on_agent_sid(BusEvent(
        type="session.agent_sid_set",
        root_id=root_id,
        sid=sid,
        payload={"mode": "native", "agent_sid": agent_sid},
        persist=False,
        seq=7003,
    ))

    await native_files._on_fork_target(BusEvent(
        type="native_files.fork_target",
        root_id=root_id,
        sid=sid,
        payload={
            "parent_app_session_id": sid,
            "fork_agent_sid": "fork-D",
            "jsonl_path": "/tmp/fork-native-paths-D.jsonl",
            "fork_agent_session_id": "fork-bc-D",
        },
        persist=False,
        seq=7002,
    ))
    await _drain_primary_resolution_tasks((sid, agent_sid))

    path = ba_home() / "sessions" / root_id / "native_paths"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_agent = {row["agent_sid"]: row for row in rows}
    codex_row = by_agent.get(agent_sid) or {}
    fork_row = by_agent.get("fork-D") or {}
    ok = (
        len(rows) == 2
        and codex_row.get("jsonl_path") == rollout_path
        and codex_row.get("trigger_event_id") == 7001
        and codex_row.get("trigger_event_type") == "session.agent_sid_set"
        and codex_row.get("can_tail") is False
        and fork_row.get("trigger_event_id") == 7002
        and fork_row.get("trigger_event_type") == "native_files.fork_target"
        and fork_row.get("fork_agent_session_id") == "fork-bc-D"
    )
    print(f"{PASS if ok else FAIL} D: native_paths append rows={len(rows)} "
          f"codex-can-tail={codex_row.get('can_tail')} "
          f"event-ids={[row.get('trigger_event_id') for row in rows]}")
    return ok


async def test_e_agent_sid_resolution_does_not_block_loop() -> bool:
    _reset_native_files()

    sess = session_manager.create(
        name="native-nonblocking", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    agent_sid = "slow-native-path-resolution"

    original = nfm._resolve_primary_jsonl

    def slow_resolve(_sess: dict, _agent_sid: str) -> Path:
        time.sleep(0.25)
        return Path("/tmp/slow-native-path-resolution.jsonl")

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        deadline = time.perf_counter() + 0.20
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.02)
            ticks += 1

    nfm._resolve_primary_jsonl = slow_resolve  # type: ignore[assignment]
    try:
        tick_task = asyncio.create_task(ticker())
        await native_files._on_agent_sid(BusEvent(
            type="session.agent_sid_set",
            root_id=root_id,
            sid=sid,
            payload={"mode": "native", "agent_sid": agent_sid},
            persist=False,
            seq=8001,
        ))
        await tick_task
        await _drain_primary_resolution_tasks((sid, agent_sid))
    finally:
        nfm._resolve_primary_jsonl = original  # type: ignore[assignment]

    ok = ticks >= 5 and agent_sid in native_files._targets.get(sid, {})
    print(f"{PASS if ok else FAIL} E: agent_sid resolution yielded loop "
          f"ticks={ticks}")
    return ok


async def test_f_demand_seed_resolution_does_not_block_loop() -> bool:
    _reset_native_files()

    sess = session_manager.create(
        name="native-demand-nonblocking", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    agent_sid = "slow-demand-path-resolution"
    session_manager.set_agent_sid(sid, "native", agent_sid)

    original = nfm._resolve_primary_jsonl

    def slow_resolve(_sess: dict, _agent_sid: str) -> Path:
        time.sleep(0.25)
        return Path("/tmp/slow-demand-path-resolution.jsonl")

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        deadline = time.perf_counter() + 0.20
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.02)
            ticks += 1

    nfm._resolve_primary_jsonl = slow_resolve  # type: ignore[assignment]
    try:
        tick_task = asyncio.create_task(ticker())
        await native_files._on_demand(BusEvent(
            type="native_files.demand",
            root_id=root_id,
            sid=sid,
            payload={
                "owning_session": sid,
                "token": "test-subscriber",
                "present": True,
            },
            persist=False,
            seq=8002,
        ))
        await tick_task
        await _drain_primary_resolution_tasks((sid, agent_sid))
    finally:
        nfm._resolve_primary_jsonl = original  # type: ignore[assignment]

    ok = (
        ticks >= 5
        and agent_sid in native_files._targets.get(sid, {})
        and sid in native_files._seeded
    )
    print(f"{PASS if ok else FAIL} F: demand seed resolution yielded loop "
          f"ticks={ticks}")
    return ok


async def test_g_concurrent_agent_sid_appends_one_native_path_row() -> bool:
    _reset_native_files()

    sess = session_manager.create(
        name="native-concurrent-paths", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    agent_sid = "concurrent-native-path"
    jsonl_path = Path("/tmp/concurrent-native-path.jsonl")

    original = nfm._resolve_primary_jsonl

    def slow_resolve(_sess: dict, _agent_sid: str) -> Path:
        time.sleep(0.05)
        return jsonl_path

    nfm._resolve_primary_jsonl = slow_resolve  # type: ignore[assignment]
    try:
        await asyncio.gather(*[
            native_files._on_agent_sid(BusEvent(
                type="session.agent_sid_set",
                root_id=root_id,
                sid=sid,
                payload={"mode": "native", "agent_sid": agent_sid},
                persist=False,
                seq=9000 + i,
            ))
            for i in range(20)
        ])
        await _drain_primary_resolution_tasks((sid, agent_sid))
    finally:
        nfm._resolve_primary_jsonl = original  # type: ignore[assignment]

    path = ba_home() / "sessions" / root_id / "native_paths"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ok = (
        len(rows) == 1
        and rows[0].get("agent_sid") == agent_sid
        and rows[0].get("jsonl_path") == str(jsonl_path)
    )
    print(f"{PASS if ok else FAIL} G: concurrent agent_sid native_paths "
          f"rows={len(rows)}")
    return ok


async def test_h_concurrent_demand_seed_appends_one_native_path_row() -> bool:
    _reset_native_files()

    sess = session_manager.create(
        name="native-concurrent-demand", model="gpt-5.5", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    agent_sid = "concurrent-demand-path"
    jsonl_path = Path("/tmp/concurrent-demand-path.jsonl")
    session_manager.set_agent_sid(sid, "native", agent_sid)

    original = nfm._resolve_primary_jsonl

    def slow_resolve(_sess: dict, _agent_sid: str) -> Path:
        time.sleep(0.05)
        return jsonl_path

    nfm._resolve_primary_jsonl = slow_resolve  # type: ignore[assignment]
    try:
        await asyncio.gather(*[
            native_files._on_demand(BusEvent(
                type="native_files.demand",
                root_id=root_id,
                sid=sid,
                payload={
                    "owning_session": sid,
                    "token": f"test-subscriber-{i}",
                    "present": True,
                },
                persist=False,
                seq=9100 + i,
            ))
            for i in range(20)
        ])
        await _drain_primary_resolution_tasks((sid, agent_sid))
    finally:
        nfm._resolve_primary_jsonl = original  # type: ignore[assignment]

    path = ba_home() / "sessions" / root_id / "native_paths"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ok = (
        len(rows) == 1
        and rows[0].get("agent_sid") == agent_sid
        and rows[0].get("jsonl_path") == str(jsonl_path)
        and sid in native_files._seeded
    )
    print(f"{PASS if ok else FAIL} H: concurrent demand seed native_paths "
          f"rows={len(rows)}")
    return ok


# ─── runner ───────────────────────────────────────────────────────

async def _run() -> int:
    native_files.bind_owner_loop(asyncio.get_running_loop())
    event_journal_writer.register(bus)
    results = [
        await test_a_primary_streaming_orphan_ingest_backup(),
        await test_b_primary_no_streaming_msg_orphan_path(),
        await test_c_worker_fork_tailer_keeps_legacy_path(),
        await test_d_native_paths_appends_discovered_targets(),
        await test_e_agent_sid_resolution_does_not_block_loop(),
        await test_f_demand_seed_resolution_does_not_block_loop(),
        await test_g_concurrent_agent_sid_appends_one_native_path_row(),
        await test_h_concurrent_demand_seed_appends_one_native_path_row(),
    ]
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} subtests passed")
    return 0 if passed == total else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
