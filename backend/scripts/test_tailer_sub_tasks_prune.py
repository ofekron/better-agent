"""Test ClaudeJsonlTailer._prune_done_sub_tasks — completed sub-tailer
tasks are dropped from `_sub_tasks` so the list can't grow without bound
over a long-lived tailer (one task is spawned per
Agent/Task subagent call). Pending tasks are kept; a crashed sub-tailer's
exception is retrieved (no 'never retrieved' warning).

Run with:
    cd backend && .venv/bin/python scripts/test_tailer_sub_tasks_prune.py
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_BC_HOME = _test_home.isolate("bc-subtask-test-")
atexit.register(lambda: shutil.rmtree(_BC_HOME, ignore_errors=True))

import jsonl_tailer as jt  # noqa: E402
from jsonl_tailer import ClaudeJsonlTailer  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _make_tailer(name: str = "x") -> ClaudeJsonlTailer:
    return ClaudeJsonlTailer(
        path=Path(_BC_HOME) / f"{name}.jsonl",
        start_offset=0,
        dispatch=lambda ev: None,
        on_cursor_advance=None,
    )


async def _scenario() -> bool:
    t = _make_tailer()

    async def ok() -> None:
        return None

    async def boom() -> None:
        raise ValueError("sub-tailer crashed")

    async def pending() -> None:
        await asyncio.sleep(60)

    d_ok = asyncio.create_task(ok())
    d_boom = asyncio.create_task(boom())
    p = asyncio.create_task(pending())
    await asyncio.sleep(0.05)   # let the two finish

    t._sub_tasks = [d_ok, d_boom, p]
    t._prune_done_sub_tasks()
    if t._sub_tasks != [p]:
        print(f"  expected only the pending task to remain, got {t._sub_tasks}")
        p.cancel()
        return False

    # Simulate many completed subagents across turns → must stay bounded.
    extra = [asyncio.create_task(ok()) for _ in range(200)]
    await asyncio.sleep(0.05)
    t._sub_tasks = [p, *extra]
    t._prune_done_sub_tasks()
    if t._sub_tasks != [p]:
        print(f"  list not bounded after 200 completed subagents: "
              f"{len(t._sub_tasks)} retained")
        p.cancel()
        return False

    # A cancelled task is also pruned.
    p.cancel()
    try:
        await p
    except BaseException:
        pass
    t._prune_done_sub_tasks()
    if t._sub_tasks != []:
        print("  cancelled task not pruned")
        return False
    return True


def test_prune_bounds_sub_tasks() -> bool:
    return asyncio.run(_scenario())


async def _duplicate_spawn_scenario() -> bool:
    original_run = ClaudeJsonlTailer.run
    started = 0

    async def fake_run(self) -> None:
        nonlocal started
        started += 1
        await asyncio.sleep(60)

    ClaudeJsonlTailer.run = fake_run
    ClaudeJsonlTailer._active_sub_tailer_keys.clear()
    first = _make_tailer()
    second = _make_tailer()
    jsonl_path = Path(_BC_HOME) / "agent-a.jsonl"
    jsonl_path.write_text("", encoding="utf-8")
    try:
        first._spawn_sub_tailer("a", jsonl_path, "tool-1", "general-purpose")
        second._spawn_sub_tailer("a", jsonl_path, "tool-1", "general-purpose")
        await asyncio.sleep(0.05)
        if started != 1:
            print(f"  expected one active sub-tailer, started={started}")
            return False
        if len(first._sub_tasks) != 1 or second._sub_tasks:
            print(
                "  duplicate task retained: "
                f"first={len(first._sub_tasks)} second={len(second._sub_tasks)}"
            )
            return False
        first._sub_tasks[0].cancel()
        try:
            await first._sub_tasks[0]
        except BaseException:
            pass
        await asyncio.sleep(0.05)
        if ClaudeJsonlTailer._active_sub_tailer_keys:
            print("  active sub-tailer key not released")
            return False
        second._spawn_sub_tailer("a", jsonl_path, "tool-1", "general-purpose")
        await asyncio.sleep(0.05)
        if started != 2 or len(second._sub_tasks) != 1:
            print(f"  respawn after release failed: started={started}")
            return False
        return True
    finally:
        for tailer in (first, second):
            for task in tailer._sub_tasks:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except BaseException:
                    pass
        ClaudeJsonlTailer._active_sub_tailer_keys.clear()
        ClaudeJsonlTailer.run = original_run


def test_duplicate_sub_tailer_spawn_is_suppressed() -> bool:
    return asyncio.run(_duplicate_spawn_scenario())


def test_subagent_scan_backoff_and_stale_pending() -> bool:
    t = _make_tailer("backoff")
    t._SUB_DIR_POLL_INTERVAL = 0.01
    t._SUB_DIR_IDLE_POLL_INTERVAL = 0.08
    t._SUB_DIR_IDLE_BACKOFF = 2.0
    if t._next_subagent_poll_interval(0.01, active=False) != 0.02:
        print("  idle interval did not back off")
        return False
    if t._next_subagent_poll_interval(0.08, active=False) != 0.08:
        print("  idle interval exceeded max")
        return False
    if t._next_subagent_poll_interval(0.08, active=True) != 0.01:
        print("  active interval did not reset to fast poll")
        return False

    t.subagent_registry.register("tool-1", "general-purpose", "work")
    t._subagent_pending_fast_until = time.monotonic() + 60
    if not t._has_fresh_subagent_pending():
        print("  fresh pending work did not stay fast")
        return False
    t._subagent_pending_fast_until = time.monotonic() - 1
    if t._has_fresh_subagent_pending():
        print("  stale pending work stayed fast forever")
        return False
    return True


async def _scan_submission_bound_scenario() -> bool:
    tailers = [_make_tailer(f"scan-bound-{i}") for i in range(8)]
    block = threading.Event()
    submitted = 0
    original_submit = jt._SUBAGENT_SCAN_EXECUTOR.submit
    original_semaphores = dict(jt._SUBAGENT_SCAN_SEMAPHORES)
    jt._SUBAGENT_SCAN_SEMAPHORES.clear()

    def submit_wrapper(fn, *args, **kwargs):
        nonlocal submitted
        submitted += 1
        return original_submit(fn, *args, **kwargs)

    def slow_scan(*_args):
        block.wait(0.2)
        return [], [], []

    for tailer in tailers:
        tailer._SUB_DIR_POLL_INTERVAL = 0.01
        tailer._SUB_DIR_IDLE_POLL_INTERVAL = 0.02
        tailer._SUB_DIR_IDLE_BACKOFF = 2.0
        tailer._scan_subagent_files = slow_scan  # type: ignore[method-assign]

    jt._SUBAGENT_SCAN_EXECUTOR.submit = submit_wrapper  # type: ignore[method-assign]
    tasks = [asyncio.create_task(t._watch_subagents()) for t in tailers]
    try:
        await asyncio.sleep(0.05)
        if submitted > jt._SUBAGENT_SCAN_MAX_PENDING_FUTURES:
            print(
                "  too many executor scans submitted before bound applied: "
                f"{submitted}"
            )
            return False
        return True
    finally:
        block.set()
        for tailer in tailers:
            tailer._stop_event.set()
            tailer._wake_subagent_scan()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:
                pass
        jt._SUBAGENT_SCAN_EXECUTOR.submit = original_submit  # type: ignore[method-assign]
        jt._SUBAGENT_SCAN_SEMAPHORES.clear()
        jt._SUBAGENT_SCAN_SEMAPHORES.update(original_semaphores)


def test_subagent_scan_submission_is_globally_bounded() -> bool:
    return asyncio.run(_scan_submission_bound_scenario())


async def _idle_without_pending_skips_executor_scenario() -> bool:
    t = _make_tailer("idle-skip")
    t._SUB_DIR_POLL_INTERVAL = 0.01
    t._SUB_DIR_IDLE_POLL_INTERVAL = 0.02
    t._SUB_DIR_IDLE_BACKOFF = 2.0
    submitted = 0

    def fail_scan(*_args):
        nonlocal submitted
        submitted += 1
        return [], [], []

    t._scan_subagent_files = fail_scan  # type: ignore[method-assign]
    task = asyncio.create_task(t._watch_subagents())
    try:
        await asyncio.sleep(0.08)
        if submitted != 0:
            print(f"  idle watcher submitted executor scans without pending work: {submitted}")
            return False
        return True
    finally:
        t._stop_event.set()
        t._wake_subagent_scan()
        task.cancel()
        try:
            await task
        except BaseException:
            pass


def test_idle_without_pending_skips_executor_scans() -> bool:
    return asyncio.run(_idle_without_pending_skips_executor_scenario())


async def _idle_wakeup_discovery_scenario() -> bool:
    t = _make_tailer("idle-parent")
    t._SUB_DIR_POLL_INTERVAL = 0.01
    t._SUB_DIR_IDLE_POLL_INTERVAL = 0.2
    t._SUB_DIR_IDLE_BACKOFF = 2.0
    spawned: list[tuple[str, Path, str, str]] = []

    def fake_spawn(agent_id, jsonl_path, parent_tuid, agent_type):
        spawned.append((agent_id, jsonl_path, parent_tuid, agent_type))

    t._spawn_sub_tailer = fake_spawn  # type: ignore[method-assign]
    task = asyncio.create_task(t._watch_subagents())
    try:
        await asyncio.sleep(0.05)
        sub_dir = t._subagents_dir()
        sub_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = sub_dir / "agent-a.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        (sub_dir / "agent-a.meta.json").write_text(
            '{"agentType":"general-purpose","description":"work"}',
            encoding="utf-8",
        )
        t.subagent_registry.register("tool-1", "general-purpose", "work")
        t._mark_subagent_pending_fast()
        deadline = time.monotonic() + 0.5
        while not spawned and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not spawned:
            print("  backed-off watcher did not discover pending subagent")
            return False
        agent_id, found_path, parent_tuid, agent_type = spawned[0]
        if (
            agent_id != "a"
            or found_path != jsonl_path
            or parent_tuid != "tool-1"
            or agent_type != "general-purpose"
        ):
            print(f"  wrong spawned subagent tuple: {spawned[0]}")
            return False
        return True
    finally:
        t._stop_event.set()
        t._wake_subagent_scan()
        task.cancel()
        try:
            await task
        except BaseException:
            pass


def test_backed_off_watcher_still_discovers_subagents() -> bool:
    return asyncio.run(_idle_wakeup_discovery_scenario())


async def _known_workflow_keeps_scanning_scenario() -> bool:
    t = _make_tailer("workflow-parent")
    t._SUB_DIR_POLL_INTERVAL = 0.01
    t._SUB_DIR_IDLE_POLL_INTERVAL = 0.02
    t._SUB_DIR_IDLE_BACKOFF = 2.0
    spawned: list[tuple[str, Path, str, str]] = []

    def fake_spawn(agent_id, jsonl_path, parent_tuid, agent_type):
        spawned.append((agent_id, jsonl_path, parent_tuid, agent_type))

    t._spawn_sub_tailer = fake_spawn  # type: ignore[method-assign]
    sub_dir = t._subagents_dir()
    wf_path = sub_dir / "workflows" / "wf_1"
    wf_path.mkdir(parents=True, exist_ok=True)
    t.subagent_registry.register("workflow-tool", "Workflow", "run workflow")
    task = asyncio.create_task(t._watch_subagents())
    try:
        deadline = time.monotonic() + 0.5
        while str(wf_path) not in t._known_workflow_dirs and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if str(wf_path) not in t._known_workflow_dirs:
            print("  workflow dir was not bound through pending Workflow claim")
            return False

        jsonl_path = wf_path / "agent-a.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        (wf_path / "agent-a.meta.json").write_text("{}", encoding="utf-8")
        deadline = time.monotonic() + 0.5
        while not spawned and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not spawned:
            print("  known workflow dir stopped scanning before later agent meta")
            return False
        agent_id, found_path, parent_tuid, agent_type = spawned[0]
        if (
            agent_id != "a"
            or found_path != jsonl_path
            or parent_tuid != "workflow-tool"
            or agent_type != "workflow-subagent"
        ):
            print(f"  wrong workflow spawned tuple: {spawned[0]}")
            return False
        return True
    finally:
        t._stop_event.set()
        t._wake_subagent_scan()
        task.cancel()
        try:
            await task
        except BaseException:
            pass


def test_known_workflow_keeps_scanning_for_late_agents() -> bool:
    return asyncio.run(_known_workflow_keeps_scanning_scenario())


TESTS = [
    ("done sub-tailer tasks pruned; pending kept; list stays bounded",
     test_prune_bounds_sub_tasks),
    ("duplicate concurrent sub-tailer spawn suppressed",
     test_duplicate_sub_tailer_spawn_is_suppressed),
    ("subagent scan backs off and stale pending expires",
     test_subagent_scan_backoff_and_stale_pending),
    ("subagent scan submissions are globally bounded",
     test_subagent_scan_submission_is_globally_bounded),
    ("idle watcher skips executor scans without pending work",
     test_idle_without_pending_skips_executor_scans),
    ("backed-off watcher still discovers subagents",
     test_backed_off_watcher_still_discovers_subagents),
    ("known workflow keeps scanning for late agent metas",
     test_known_workflow_keeps_scanning_for_late_agents),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
