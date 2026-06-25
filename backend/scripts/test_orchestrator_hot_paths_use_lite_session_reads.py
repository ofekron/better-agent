"""Static regression lock: orchestrator hot paths must use
`session_manager.get_lite`, never the full `session_manager.get`, when
they only read top-level session fields.

RCA (lag-watchdog main-loop block, escalating to a 19s freeze):
`Coordinator.handle_prompt` called `session_manager.get(app_session_id)`
to read a handful of top-level fields (disallowed_tools /
orchestration_mode / cwd / model / supervisor_enabled) plus a messages
truthiness check. `get()` deep-copies the ENTIRE session tree —
including every message's events list and worker-panel events — under
the per-root lock; on a large hydrated session that deepcopy held the
main asyncio event loop for ~19s (faulthandler dump pinned the block at
`session_manager.py:2052`, mid `copy.deepcopy`). `get_lite()` strips the
event lists and is the documented reader for this exact caller shape
(see `get_lite`'s docstring). handle_prompt never reads msg.events, so
get_lite is behavior-equivalent and ~orders-of-magnitude cheaper.

This mirrors `test_main_hot_paths_use_lite_session_reads.py` but for
`orchestrator.py`. It AST-scans the named functions and asserts each
calls `get_lite` and does NOT call `get`. It FAILS before the fix
(handle_prompt called get, not get_lite) and PASSES after.

Run with:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_orchestrator_hot_paths_use_lite_session_reads.py
"""

from __future__ import annotations

import ast
import os
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
ORCH = BACKEND / "orchestrator.py"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _functions_by_name(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _session_manager_calls(node: ast.AST, method: str) -> list[int]:
    lines: list[int] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != method:
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == "session_manager":
            lines.append(call.lineno)
    return lines


def _run() -> bool:
    tree = ast.parse(ORCH.read_text(), filename=os.fspath(ORCH))
    funcs = _functions_by_name(tree)
    results: list[tuple[str, bool, str]] = []

    # handle_prompt reads only top-level fields + a messages truthiness
    # check (never msg.events) — must use get_lite, never the full get().
    no_full_get = ["handle_prompt"]
    for name in no_full_get:
        node = funcs.get(name)
        lines = _session_manager_calls(node, "get") if node else []
        results.append((
            f"{name} avoids session_manager.get",
            node is not None and not lines,
            f"lines={lines}" if node else "missing function",
        ))

    lite_required = ["handle_prompt"]
    for name in lite_required:
        node = funcs.get(name)
        lite = _session_manager_calls(node, "get_lite") if node else []
        results.append((
            f"{name} uses get_lite",
            bool(node and lite),
            f"get_lite lines={lite}" if node else "missing function",
        ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    return 0 if _run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
