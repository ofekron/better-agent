"""Regression: every `set_agent_sid(...)` call site must pass keyword
arguments that ACTUALLY exist on `SessionManager.set_agent_sid`.

A stale `set_agent_sid(..., claude_sid=...)` call in
`orchs/manager/_delegation.py` (the param was renamed to `agent_sid`)
raised `TypeError` at runtime — but it sat inside a `try/except
logger.exception`, so the fork BC silently never got its CLI sid stamped,
cascading into a failed delegation approval flow. It went unnoticed
because the only thing that exercises it is the manager-delegation E2E,
which couldn't run (REST 401). This AST guard catches the whole class
statically — no subprocess needed.
"""
from __future__ import annotations

import ast
import inspect
import os

import _test_home

_test_home.isolate("ba-set-agent-sid-kwargs-")

from session_manager import SessionManager  # noqa: E402

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _accepted_kwargs() -> set[str]:
    sig = inspect.signature(SessionManager.set_agent_sid)
    return {
        name for name, p in sig.parameters.items()
        if name != "self"
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }


def _iter_backend_py() -> list[str]:
    out = []
    for root, _dirs, files in os.walk(_BACKEND):
        if ".venv" in root or "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return out


def test_all_set_agent_sid_call_sites_use_valid_kwargs() -> None:
    accepted = _accepted_kwargs()
    violations: list[str] = []

    for path in _iter_backend_py():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "set_agent_sid"):
                continue
            for kw in node.keywords:
                if kw.arg is None:  # **kwargs splat — can't check
                    continue
                if kw.arg not in accepted:
                    rel = os.path.relpath(path, _BACKEND)
                    violations.append(f"{rel}:{node.lineno} -> {kw.arg}=")

    assert not violations, (
        f"stale set_agent_sid kwargs (accepted: {sorted(accepted)}):\n  "
        + "\n  ".join(violations)
    )
