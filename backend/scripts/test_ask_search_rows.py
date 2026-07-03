from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-ask-search-rows-")

import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import session_search  # noqa: E402


def test_rows_for_ids_ranked_order_and_missing_dropped() -> None:
    a = session_manager.create(
        name="alpha", model="sonnet", cwd="/tmp/ask-rows",
        orchestration_mode="native", source="cli",
    )
    b = session_manager.create(
        name="beta", model="sonnet", cwd="/tmp/ask-rows",
        orchestration_mode="native", source="cli",
    )
    rows = main._ask_search_rows_for_ids([b["id"], "missing-id", a["id"]])
    assert [r["id"] for r in rows] == [b["id"], a["id"]]
    assert rows[0]["name"] == "beta"
    assert rows[1]["name"] == "alpha"
    assert main._ask_search_rows_for_ids([]) == []


def test_search_endpoint_returns_rows_for_matches() -> None:
    s = session_manager.create(
        name="gamma", model="sonnet", cwd="/tmp/ask-rows",
        orchestration_mode="native", source="cli",
    )

    async def fake_search(query: str, **kwargs) -> dict:
        return {"session_ids": [s["id"]], "reasoning": "r", "error": None}

    orig_search = session_search.run_search_sessions_session
    orig_require = main._require_ask_internal
    session_search.run_search_sessions_session = fake_search
    main._require_ask_internal = lambda token: None
    try:
        result = asyncio.run(
            main.internal_ask_ui_search_sessions(
                body={"query": "gamma"}, x_internal_token="t",
            )
        )
    finally:
        session_search.run_search_sessions_session = orig_search
        main._require_ask_internal = orig_require

    assert result["session_ids"] == [s["id"]]
    # The frontend session list is paginated; the endpoint must return
    # full rows for every match so unloaded sessions still render.
    assert [r["id"] for r in result["sessions"]] == [s["id"]]
    assert result["sessions"][0]["name"] == "gamma"


if __name__ == "__main__":
    test_rows_for_ids_ranked_order_and_missing_dropped()
    test_search_endpoint_returns_rows_for_matches()
    print("OK")
