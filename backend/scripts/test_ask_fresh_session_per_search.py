"""Regression: search-worker sessions are hidden — never listable.

Each Ask / session-bridge search spawns a FRESH ephemeral "search worker"
session (session_search.run_search_sessions_session) that is deleted after
its turn. While one is briefly alive it must NOT appear in the session
index (sidebar / search results / validate_proposed targets) — otherwise
throwaway workers would clutter the UI and could be proposed as targets.

`SEARCH_WORKER_MODE` is an unknown working_mode, so
`working_mode.should_hide_from_sidebar` returns True and `_build_index`
excludes it. This guard writes a worker-mode session to disk and asserts
`_build_index` (and `validate_proposed`) skip it.
"""
import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc_test_search_worker_")

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_search  # noqa: E402
import session_store  # noqa: E402
import working_mode  # noqa: E402


def test_search_worker_hidden_from_index() -> None:
    # A normal listable session + a search-worker session on disk.
    session_store.write_session_full(
        {
            "id": "real-session",
            "name": "Real",
            "cwd": "/p",
            "messages": [{"role": "user", "content": "hi"}],
        },
        bump_updated_at=False,
    )
    session_store.write_session_full(
        {
            "id": "search-worker-ephemeral",
            "name": "search-worker",
            "cwd": "/p",
            "messages": [{"role": "user", "content": "x"}],
            "working_mode": session_search.SEARCH_WORKER_MODE,
        },
        bump_updated_at=False,
    )

    index_ids = {s["id"] for s in session_search._build_index()}
    if "search-worker-ephemeral" in index_ids:
        raise AssertionError(
            f"search worker leaked into index: {index_ids}"
        )
    if "real-session" not in index_ids:
        raise AssertionError(
            f"real session missing from index: {index_ids}"
        )
    # validate_proposed must reject the worker id as a target.
    if session_search.validate_proposed(["search-worker-ephemeral"]) != []:
        raise AssertionError("search worker accepted as a propose target")
    # Sanity: should_hide_from_sidebar agrees.
    if not working_mode.should_hide_from_sidebar(
        {"working_mode": session_search.SEARCH_WORKER_MODE}
    ):
        raise AssertionError("SEARCH_WORKER_MODE not hidden by should_hide_from_sidebar")


if __name__ == "__main__":
    test_search_worker_hidden_from_index()
    print("OK: search-worker sessions are hidden from the index")
