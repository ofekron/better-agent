"""Integration test for AI-driven session search — REAL claude CLI.

Seeds 3 dummy sessions in an isolated BETTER_CLAUDE_HOME, fires a
natural-language query, and asserts the relevant session id appears
in the model's response. Skips cleanly (exit 0) when:
  * `claude` is not on PATH (CI containers without the CLI)
  * No claude provider can be configured (subscription auth missing)
  * The provider call returns `error=provider_failed` (auth issue)

Run with:
    cd backend && .venv/bin/python scripts/integration_test_session_search.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import shutil as _shutil
import sys
import tempfile
from pathlib import Path


import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-search-int-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
SKIP = "\x1b[33mSKIP\x1b[0m"


def _seed_sessions() -> None:
    """Write three distinct dummy root sessions to the tempdir."""
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    seeds = [
        {
            "id": "auth-session-id",
            "name": "Refactor login flow",
            "cwd": "/tmp/myproj",
            "updated_at": "2026-05-03T00:00:00",
            "messages": [
                {"role": "user", "content": "I need to refactor the authentication flow — JWT tokens are expiring incorrectly."},
            ],
        },
        {
            "id": "redis-session-id",
            "name": "Redis connection drop",
            "cwd": "/tmp/myproj",
            "updated_at": "2026-05-02T00:00:00",
            "messages": [
                {"role": "user", "content": "Debug intermittent redis disconnects in the worker pool."},
            ],
        },
        {
            "id": "ui-session-id",
            "name": "Sidebar styling",
            "cwd": "/tmp/myproj",
            "updated_at": "2026-05-01T00:00:00",
            "messages": [
                {"role": "user", "content": "Polish the sidebar — fonts, spacing, dark-mode colors."},
            ],
        },
    ]
    for s in seeds:
        (sessions_dir / f"{s['id']}.json").write_text(json.dumps(s))


def _ensure_claude_provider() -> bool:
    """Create a default subscription claude provider if none exists.
    Returns False when we can't bootstrap one (caller skips).
    """
    import config_store
    state = config_store._load_state()
    if state.get("providers"):
        return True
    config_store.add_provider({
        "name": "claude (test)",
        "kind": "claude",
        "mode": "subscription",
    })
    return True


def test_search_returns_relevant_id() -> int:
    """Returns 0 on PASS, 1 on FAIL, 2 on SKIP."""
    if _shutil.which("claude") is None:
        print(f"{SKIP} claude CLI not on PATH — search integration test skipped")
        return 2

    _seed_sessions()
    if not _ensure_claude_provider():
        print(f"{SKIP} could not configure a claude provider")
        return 2

    import session_search

    out = asyncio.run(session_search.search(
        "find the session about authentication / login refactoring",
        timeout=90.0,
    ))
    if out.get("error") == "provider_failed":
        print(f"{SKIP} claude provider call failed (auth?) — output={out!r}")
        return 2
    if out.get("error") not in (None, "parse_failed"):
        print(f"{FAIL} unexpected error: {out!r}")
        return 1

    ids = out.get("session_ids") or []
    if "auth-session-id" not in ids:
        print(f"{FAIL} expected auth-session-id in {ids!r}; reasoning={out.get('reasoning')!r}")
        return 1
    print(f"{PASS} integration: auth session id in results ({ids}) — reasoning: {out.get('reasoning')!r}")
    return 0


def main() -> int:
    try:
        rc = test_search_returns_relevant_id()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if rc == 2:
        # Skip → exit 0 so CI doesn't fail when CLI is absent.
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
