"""Regression checks for provider spawn hot-path reads.

Run with:
    cd backend && .venv/bin/python scripts/test_provider_spawn_hot_path.py
"""

from __future__ import annotations

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_claude  # noqa: E402
from session_manager import SessionManager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(name: str, ok: bool, detail: str = "") -> tuple[str, bool, str]:
    tag = PASS if ok else FAIL
    print(f"  {tag} {name}{'' if ok else ' - ' + detail}")
    return name, ok, detail


def test_claude_spawn_reads_run_config_without_full_session_copy() -> bool:
    source = inspect.getsource(provider_claude.ClaudeProvider.start_run)
    get_fields_pos = source.find("_sm.get_fields(app_session_id")
    get_pos = source.find("_sm.get(app_session_id")
    return get_fields_pos >= 0 and (get_pos < 0 or get_fields_pos < get_pos)


def test_session_manager_has_field_snapshot_reader() -> bool:
    source = inspect.getsource(SessionManager.get_fields)
    return (
        "hydrate_events=False" in source
        and "session_store._find_in_tree" in source
        and "copy.deepcopy(node.get(field))" in source
    )


def main() -> int:
    results = [
        _check(
            "Claude spawn uses field snapshots",
            test_claude_spawn_reads_run_config_without_full_session_copy(),
            "provider_claude.start_run deep-copies the full session",
        ),
        _check(
            "field snapshot reader skips event hydration",
            test_session_manager_has_field_snapshot_reader(),
            "session field reads may hydrate/copy message events",
        ),
    ]
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
