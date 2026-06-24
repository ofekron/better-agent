"""Regression-locks the lazy-hydrate gate after the manager/native merge.

`SessionManager._native_only_tree` gates the cheap `hydrate_events=False`
cold-load path. The merge made a manager session structurally identical
to a native one (no more `msg.manager`, single `agent_session_id`), so
the gate MUST key on `orchestration_mode` first — otherwise a manager
session with worker panels would wrongly take the native lazy path.

This asserts:
  - A manager-mode tree returns False (never the native lazy path),
    even with no worker panels.
  - A native tree with worker panels returns False.
  - A plain native tree returns True.

Run with:
    cd backend && .venv/bin/python scripts/test_native_only_tree_manager_guard.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-tree-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_nt = session_manager._native_only_tree


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    results.append((
        "manager-mode tree (no workers) is NOT native-only",
        _nt({"orchestration_mode": "manager", "messages": [{"id": "m"}]}) is False,
        "returned True — manager would take the native lazy-hydrate path",
    ))
    results.append((
        "native tree WITH worker panels is NOT native-only",
        _nt({
            "orchestration_mode": "native",
            "messages": [{"id": "m", "workers": [{"delegation_id": "d"}]}],
        }) is False,
        "returned True — worker panels need full hydrate",
    ))
    results.append((
        "plain native tree IS native-only",
        _nt({
            "orchestration_mode": "native",
            "messages": [{"id": "m", "events": [], "workers": []}],
        }) is True,
        "returned False — plain native should take the lazy path",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + detail}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
