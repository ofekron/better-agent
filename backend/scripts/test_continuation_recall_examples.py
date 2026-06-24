from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-continuation-recall-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import continuation_recall_mcp  # noqa: E402
import runner_codex  # noqa: E402
import session_recall  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _seed_session() -> str:
    sess = session_manager.create(name="continuation examples", cwd="/tmp")
    sid = sess["id"]
    examples = [
        (
            "user",
            "is continuation claude only or all providers?",
        ),
        (
            "assistant",
            "The coordinator continuation trigger is provider-generic, but "
            "real recall support must be wired per provider. Claude has "
            "recall_history through SDK MCP, Codex uses a dynamic tool, and "
            "Gemini needs a run-local MCP server.",
        ),
        (
            "user",
            "If continuation happens how we can know it?",
        ),
        (
            "assistant",
            "You can know continuation happened from the Auto continuing UI "
            "pill, the session continuation_chain, backend logs, and the "
            "retry run input.json where session_id is null and "
            "continuation_chain is populated.",
        ),
        (
            "user",
            "why gemini cant have recall_history tool?",
        ),
        (
            "assistant",
            "Gemini can have recall_history. It is not impossible; it just "
            "needs a temporary continuation-recall MCP server injected into "
            "Gemini mcpServers for the continuation run.",
        ),
    ]
    for idx, (role, content) in enumerate(examples):
        msg = {
            "id": f"m-{idx}",
            "role": role,
            "content": content,
            "events": [],
            "isStreaming": False,
        }
        if role == "user":
            session_manager.append_user_msg(sid, msg)
        else:
            session_manager.append_assistant_msg(sid, msg)
    return sid


def _recall_payload(sid: str, query: str, k: int = 5) -> dict:
    session_recall.build_index(sid)
    return {"results": session_recall.recall(sid, query, k=k)}


def _results_text(payload: dict) -> str:
    return "\n".join(str(item.get("text") or "") for item in payload.get("results") or [])


def test_direct_recall_answers_session_questions(sid: str) -> None:
    payload = _recall_payload(sid, "Can Gemini continuation use recall_history?", k=5)
    text = _results_text(payload)
    assert "Gemini can have recall_history" in text
    assert "temporary continuation-recall MCP server" in text

    payload = _recall_payload(sid, "How do I know continuation happened?", k=5)
    text = _results_text(payload)
    assert "Auto continuing UI pill" in text
    assert "continuation_chain" in text
    assert "input.json" in text


def test_codex_recall_history_tool_answers_session_questions(sid: str) -> None:
    original = runner_codex._post_loopback_sync

    def fake_post(payload: dict, **kwargs) -> dict:
        assert kwargs["url_path"] == "/api/internal/continuation-recall"
        assert payload["app_session_id"] == sid
        return _recall_payload(sid, payload["query"], k=payload.get("k") or 5)

    runner_codex._post_loopback_sync = fake_post
    try:
        handler = runner_codex._build_recall_tool_handler(
            app_session_id=sid,
            backend_url="http://127.0.0.1:8000",
            internal_token="token",
        )
        result = asyncio.run(handler({
            "arguments": {
                "query": "Which providers have continuation recall tools?",
                "k": 5,
            },
        }))
    finally:
        runner_codex._post_loopback_sync = original
    assert result["success"] is True
    payload = json.loads(result["contentItems"][0]["text"])
    text = _results_text(payload)
    assert "Claude has recall_history" in text
    assert "Codex uses a dynamic tool" in text
    assert "Gemini needs a run-local MCP server" in text


def test_gemini_recall_history_tool_answers_session_questions(sid: str) -> None:
    original = continuation_recall_mcp._post_recall

    def fake_post(query: str, k: int) -> dict:
        return _recall_payload(sid, query, k=k)

    continuation_recall_mcp._post_recall = fake_post
    try:
        payload = continuation_recall_mcp.recall_history_response(
            "What tells us continuation happened?",
            5,
        )
    finally:
        continuation_recall_mcp._post_recall = original
    text = _results_text(payload)
    assert "Auto continuing UI pill" in text
    assert "continuation_chain" in text


def test_continuation_active_can_clear(sid: str) -> None:
    session_manager.set_msg_continuation_active(sid, "m-1", 1)
    active = session_manager.get(sid)["messages"][1].get("continuation_active")
    assert active == 1, active
    session_manager.set_msg_continuation_active(sid, "m-1", None)
    cleared = session_manager.get(sid)["messages"][1]
    assert "continuation_active" not in cleared, cleared


def main() -> int:
    try:
        sid = _seed_session()
        test_direct_recall_answers_session_questions(sid)
        test_codex_recall_history_tool_answers_session_questions(sid)
        test_gemini_recall_history_tool_answers_session_questions(sid)
        test_continuation_active_can_clear(sid)
        print("ALL TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
