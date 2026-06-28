#!/usr/bin/env python3
"""Regression guard: runner._run must assign `_bare` before any use.

This catches the exact failure:
    UnboundLocalError: cannot access local variable '_bare' where it is not associated with a value

Run with:
    cd backend && .venv/bin/python scripts/test_runner_bare_assignment_order.py
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner.py"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def main() -> None:
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"), filename=str(RUNNER))
    run_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run"
        ),
        None,
    )
    check(run_fn is not None, "found async runner._run")

    stores: list[int] = []
    loads: list[int] = []
    for node in ast.walk(run_fn):
        if isinstance(node, ast.Name) and node.id == "_bare":
            if isinstance(node.ctx, ast.Store):
                stores.append(node.lineno)
            elif isinstance(node.ctx, ast.Load):
                loads.append(node.lineno)

    check(bool(stores), "_run assigns _bare")
    check(bool(loads), "_run uses _bare")

    first_store = min(stores)
    first_load = min(loads)
    check(first_store < first_load, "_bare is assigned before first use")

    top_level_store_lines = {
        stmt.lineno
        for stmt in run_fn.body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
        for node in ast.walk(stmt)
        if isinstance(node, ast.Name)
        and node.id == "_bare"
        and isinstance(node.ctx, ast.Store)
    }
    check(
        first_store in top_level_store_lines,
        "first _bare assignment is unconditional at _run body level",
    )


if __name__ == "__main__":
    main()
