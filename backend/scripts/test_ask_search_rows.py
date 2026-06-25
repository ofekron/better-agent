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
import assistant_ui  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import session_search  # noqa: E402


def test_search_endpoint_returns_rows_for_matches() -> None:
    s = session_manager.create(
        name="gamma", model="sonnet", cwd="/tmp/ask-rows",
        orchestration_mode="native", source="cli", user_initiated=True,
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

    assert set(result) == {"results", "reasoning"}
    assert [r["id"] for r in result["results"]] == [s["id"]]
    assert result["results"][0]["name"] == "gamma"


def test_assistant_search_uses_same_canonical_contract() -> None:
    s = session_manager.create(
        name="assistant target", model="sonnet", cwd="/tmp/assistant-search",
        orchestration_mode="native", source="cli", user_initiated=True,
    )

    async def fake_search(query: str, **kwargs) -> dict:
        return {"session_ids": [s["id"]], "reasoning": "best", "error": None}

    orig_search = session_search.run_search_sessions_session
    session_search.run_search_sessions_session = fake_search
    try:
        result = asyncio.run(assistant_ui.search("assistant target"))
    finally:
        session_search.run_search_sessions_session = orig_search

    assert set(result) == {"results", "reasoning"}
    assert result["results"][0]["id"] == s["id"]
    assert result["results"][0]["name"] == "assistant target"


if __name__ == "__main__":
    test_search_endpoint_returns_rows_for_matches()
    test_assistant_search_uses_same_canonical_contract()
    print("OK")
