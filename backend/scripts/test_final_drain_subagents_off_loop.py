"""Regression test: ClaudeJsonlTailer._final_drain_subagents must not block
the event loop while scanning a subagents/ directory tree.

`_final_drain_subagents` used to call `sub_dir.glob(...)`, `.exists()`,
`.iterdir()`, and `.read_text()` directly inline inside an `async def`
method with no `asyncio.to_thread` wrapping. Live faulthandler evidence
showed the event loop stuck for up to 37.4s with the main thread's sampled
frame inside `glob.py:557 scandir` / `glob.py:469 select_wildcard`, reached
from this exact method (confirmed via 3 separate faulthandler dumps across
this session, deepening to the exact glob call on the worst-magnitude
incident).

Fix: directory discovery (glob/exists/iterdir/read_text) is now done by
`_scan_direct_subagent_metas` / `_scan_workflow_subagent_metas` — pure
functions with no side effects on shared state — invoked via
`asyncio.to_thread`. The claim()/dispatch()/`_known_meta_files` bookkeeping,
which touches shared mutable state, stays on the event loop exactly as
before.

This test proves:
  1. The loop stays responsive while the directory scan is slow (a real
     `glob()` over many files, with an injected delay standing in for
     slow/contended disk I/O).
  2. Dispatch semantics are unchanged: the correct events land in the
     correct order, `_known_meta_files` dedup still works, and a second
     call is a no-op (idempotent) for already-drained metas.
  3. A control case using the OLD bare (non-to_thread) call pattern really
     does freeze the loop, so a false pass above isn't hiding a no-op.

Run with:
    cd backend && .venv/bin/python scripts/test_final_drain_subagents_off_loop.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import paths  # noqa: E402

paths.engage_test_home(tempfile.mkdtemp(prefix="final_drain_subagents_test_"))

import jsonl_tailer  # noqa: E402
from claude_jsonl_enrich import _SubagentRegistry  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

N_AGENTS = 40
SLOW_GLOB_SECONDS = 0.6


def _write_subagent_fixture(
    tmp_root: Path, n_agents: int, registry
) -> tuple[Path, Path, list[str]]:
    """Build a `<session>/subagents/` tree with `n_agents` direct
    Agent/Task metas + jsonls, and register a matching pending claim for
    each in `registry` (mirrors the parent jsonl's Agent/Task tool_use
    having been seen first, as in live tailing). Returns
    (session_jsonl_path, sub_dir, ordered agent_ids)."""
    session_jsonl = tmp_root / "sess.jsonl"
    session_jsonl.write_text("", encoding="utf-8")
    sub_dir = tmp_root / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    agent_ids = []
    for i in range(n_agents):
        agent_id = f"a{i:03d}"
        agent_ids.append(agent_id)
        description = f"task {i}"
        registry.register(f"tool-use-{agent_id}", "general-purpose", description)
        (sub_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": description}),
            encoding="utf-8",
        )
        (sub_dir / f"agent-{agent_id}.jsonl").write_text(
            json.dumps({"uuid": f"u-{agent_id}", "type": "assistant"}) + "\n",
            encoding="utf-8",
        )
    return session_jsonl, sub_dir, agent_ids


class _SlowPath(type(Path())):
    """Path subclass whose .glob() sleeps first, standing in for a slow/
    contended directory scan (e.g. many concurrent tailers under swap
    pressure, as observed live)."""

    _slow_seconds = 0.0

    def glob(self, pattern):  # noqa: D102
        if self._slow_seconds:
            time.sleep(self._slow_seconds)
        return super().glob(pattern)


async def _count_ticks_while(coro):
    ticks = 0
    stop = False

    async def _heartbeat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.02)
            ticks += 1

    hb_task = asyncio.create_task(_heartbeat())
    result = await coro
    stop = True
    await hb_task
    return ticks, result


def _make_tailer(tmp_root: Path, registry: _SubagentRegistry, session_jsonl: Path):
    dispatched: list[dict] = []

    async def _dispatch(ev: dict) -> None:
        dispatched.append(ev)

    tailer = jsonl_tailer.ClaudeJsonlTailer(
        path=session_jsonl,
        start_offset=0,
        dispatch=_dispatch,
        subagent_registry=registry,
    )
    return tailer, dispatched


async def test_offloaded_scan_does_not_block_loop() -> bool:
    tmp_root = Path(tempfile.mkdtemp(prefix="fd_offload_"))
    registry = _SubagentRegistry()
    session_jsonl, sub_dir, agent_ids = _write_subagent_fixture(tmp_root, N_AGENTS, registry)
    tailer, dispatched = _make_tailer(tmp_root, registry, session_jsonl)

    # Force the directory-scan helper to look slow, simulating contended
    # disk I/O — this is what asyncio.to_thread must shield the loop from.
    orig_glob = jsonl_tailer._scan_direct_subagent_metas

    def _slow_scan(sub_dir_arg, known):
        time.sleep(SLOW_GLOB_SECONDS)
        return orig_glob(sub_dir_arg, known)

    jsonl_tailer._scan_direct_subagent_metas = _slow_scan
    try:
        ticks, _ = await _count_ticks_while(tailer._final_drain_subagents())
    finally:
        jsonl_tailer._scan_direct_subagent_metas = orig_glob

    ok = ticks >= 10 and len(dispatched) == N_AGENTS
    print(
        f"{PASS if ok else FAIL} _final_drain_subagents via asyncio.to_thread: "
        f"loop ticks during a {SLOW_GLOB_SECONDS}s slow scan = {ticks} "
        f"(want >=10), dispatched={len(dispatched)}/{N_AGENTS}"
    )
    return ok


async def test_direct_scan_blocks_loop_control() -> bool:
    """Control: prove the OLD (bare, non-to_thread) call pattern really
    does freeze the loop, so a false pass above isn't hiding a no-op."""
    tmp_root = Path(tempfile.mkdtemp(prefix="fd_control_"))
    registry = _SubagentRegistry()
    session_jsonl, sub_dir, agent_ids = _write_subagent_fixture(tmp_root, N_AGENTS, registry)
    tailer, dispatched = _make_tailer(tmp_root, registry, session_jsonl)

    orig_glob = jsonl_tailer._scan_direct_subagent_metas

    def _slow_scan(sub_dir_arg, known):
        time.sleep(SLOW_GLOB_SECONDS)
        return orig_glob(sub_dir_arg, known)

    async def _direct_drain():
        # Mirrors the pre-fix call site: the slow scan runs bare on the
        # loop instead of via asyncio.to_thread.
        result = _slow_scan(sub_dir, tailer._known_meta_files)
        for key, jsonl_path, meta in result:
            parent_tuid = tailer.subagent_registry.claim(
                meta.get("agentType", "") or "", meta.get("description", "") or ""
            )
            if parent_tuid is None:
                continue
            tailer._known_meta_files.add(key)
            await tailer._drain_agent_jsonl(jsonl_path, parent_tuid)

    ticks, _ = await _count_ticks_while(_direct_drain())
    ok = ticks == 0
    print(
        f"{PASS if ok else FAIL} control - bare (non-to_thread) scan: loop "
        f"ticks during a {SLOW_GLOB_SECONDS}s slow scan = {ticks} (want ==0, "
        f"proves a slow directory scan is real blocking, not a no-op)"
    )
    return ok


async def test_dedup_and_workflow_semantics_unchanged() -> bool:
    """Behavioral parity: dedup via `_known_meta_files`, claim() matching,
    and workflow-subagent binding must be unchanged by the refactor."""
    tmp_root = Path(tempfile.mkdtemp(prefix="fd_semantics_"))
    registry = _SubagentRegistry()
    session_jsonl, sub_dir, agent_ids = _write_subagent_fixture(tmp_root, 3, registry)
    tailer, dispatched = _make_tailer(tmp_root, registry, session_jsonl)

    # First drain: all 3 should dispatch and get marked known.
    await tailer._final_drain_subagents()
    ok_first = len(dispatched) == 3 and len(tailer._known_meta_files) == 3

    # Second drain (idempotency): no new meta files, no new dispatches.
    dispatched.clear()
    await tailer._final_drain_subagents()
    ok_idempotent = len(dispatched) == 0

    # Workflow subagents: create a wf_ dir with one meta, bind via claim_workflow.
    wf_dir = sub_dir / "workflows" / "wf_run1"
    wf_dir.mkdir(parents=True)
    (wf_dir / "agent-w0.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "wf task"}),
        encoding="utf-8",
    )
    (wf_dir / "agent-w0.jsonl").write_text(
        json.dumps({"uuid": "u-w0", "type": "assistant"}) + "\n", encoding="utf-8"
    )
    registry.register("parent-tool-use-1", "Workflow", "")
    dispatched.clear()
    await tailer._final_drain_subagents()
    ok_workflow = len(dispatched) == 1

    ok = ok_first and ok_idempotent and ok_workflow
    print(
        f"{PASS if ok else FAIL} dedup/idempotency/workflow-binding semantics unchanged "
        f"(first_drain={ok_first}, idempotent_replay={ok_idempotent}, "
        f"workflow_dispatch={ok_workflow})"
    )
    return ok


def test_final_drain_subagents_uses_to_thread() -> bool:
    """Static guard: `_final_drain_subagents` must route both scans through
    asyncio.to_thread, not call the sync scanners bare."""
    src = inspect.getsource(jsonl_tailer.ClaudeJsonlTailer._final_drain_subagents)
    has_direct_to_thread = "asyncio.to_thread(\n            _scan_direct_subagent_metas" in src or (
        "_scan_direct_subagent_metas" in src and "asyncio.to_thread" in src
    )
    has_workflow_to_thread = "_scan_workflow_subagent_metas" in src and src.count("asyncio.to_thread") >= 2
    bare_glob_calls = [
        line for line in src.splitlines()
        if ".glob(" in line or (".iterdir(" in line and "to_thread" not in line)
    ]
    ok = has_direct_to_thread and has_workflow_to_thread and not bare_glob_calls
    print(
        f"{PASS if ok else FAIL} _final_drain_subagents routes both directory "
        f"scans through asyncio.to_thread (bare .glob()/.iterdir() calls in "
        f"method body = {len(bare_glob_calls)})"
    )
    return ok


def main() -> int:
    results = []
    results.append(asyncio.run(test_offloaded_scan_does_not_block_loop()))
    results.append(asyncio.run(test_direct_scan_blocks_loop_control()))
    results.append(asyncio.run(test_dedup_and_workflow_semantics_unchanged()))
    results.append(test_final_drain_subagents_uses_to_thread())
    ok = all(results)
    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
