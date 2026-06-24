"""Supervisor user-facing controls stay behind the extension boundary.

Run with:
    cd backend && .venv/bin/python scripts/test_supervisor_extension_boundary.py
"""

from __future__ import annotations

import ast
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend" / "src"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _main_functions() -> dict[str, ast.AST]:
    tree = ast.parse((BACKEND / "main.py").read_text(), filename=os.fspath(BACKEND / "main.py"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _run() -> bool:
    funcs = _main_functions()
    results: list[tuple[str, bool, str]] = []
    ws_chat = funcs.get("websocket_chat")
    ws_literals = {
        node.value
        for node in ast.walk(ws_chat) if ws_chat
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for literal in ("supervisor_toggle", "review_last_work"):
        results.append((
            f"{literal} is not a public WS command",
            literal not in ws_literals,
            "found in websocket_chat",
        ))

    for name in ("internal_supervisor_toggle", "internal_supervisor_review_last_work"):
        results.append((f"{name} exists", name in funcs, "missing internal route"))

    app = (FRONTEND / "App.tsx").read_text()
    hook = (FRONTEND / "hooks" / "useWebSocket.ts").read_text()
    results.extend([
        (
            "frontend supervisor toggle uses extension route",
            "/supervisor-toggle" in app and "supervisor_toggle" not in hook,
            "missing extension route or old WS command remains",
        ),
        (
            "frontend supervisor review uses extension route",
            "/review-last-work" in app and "review_last_work" not in hook,
            "missing extension route or old WS command remains",
        ),
    ])

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _run() else 1)
