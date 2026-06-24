"""Tests for native session import (backend/native_import.py).

Covers:
  - claude native jsonl enumeration + full ingest with multi-turn
    segmentation (user prompt, assistant text, tool_use, tool_result,
    assistant text; then a second turn).
  - imported session has correctly segmented user/assistant messages
    with events applied through the shared apply_event funnel.
  - idempotency: re-importing the same native session is a no-op that
    returns the existing root_id and creates no extra session.

Run with:
    cd backend && .venv/bin/python scripts/test_native_import.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import native_import  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

CLAUDE_CONFIG_DIR = Path(_TMP_HOME) / "claude-home"
os.environ["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)


def _line(ltype: str, *, role: str, content, parent: str | None = None) -> str:
    return json.dumps({
        "type": ltype,
        "uuid": str(uuid.uuid4()),
        **({"parentUuid": parent} if parent else {}),
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": role, "content": content},
    })


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _write_claude_session(encoded_cwd: str, sid: str, lines: list[str]) -> Path:
    d = CLAUDE_CONFIG_DIR / "projects" / encoded_cwd
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _make_two_turn_jsonl() -> list[str]:
    """Two user turns. The tool_result line is role=user but must NOT
    split a new turn."""
    return [
        _line("user", role="user", content=[_text("What is 2+2?")]),
        _line("assistant", role="assistant", content=[_text("Let me check.")]),
        _line("assistant", role="assistant", content=[
            {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "echo 4"}},
        ]),
        _line("user", role="user", content=[
            {"type": "tool_result", "tool_use_id": "tu1", "content": "4"},
        ]),
        _line("assistant", role="assistant", content=[_text("2+2 = 4")]),
        _line("user", role="user", content=[_text("Thanks!")]),
        _line("assistant", role="assistant", content=[_text("You're welcome.")]),
    ]


def test_claude_enumerate_and_import():
    sid = "abc123"
    _write_claude_session("encoded-cwd", sid, _make_two_turn_jsonl())

    sessions = native_import.enumerate_native_sessions()
    matches = [s for s in sessions if s.provider_kind == "claude" and s.native_id == sid]
    assert len(matches) == 1, f"expected 1 claude session, got {matches}"
    sess = matches[0]
    assert sess.jsonl_path.endswith(f"{sid}.jsonl")

    root_id = native_import.import_session(sess)
    loaded = session_manager.get(root_id)
    assert loaded is not None, "imported session not found"
    msgs = loaded["messages"]
    # 2 user + 2 assistant, strictly alternating starting with user.
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"], f"roles={roles}"

    # Title derived from the first prompt.
    assert loaded["name"].startswith("What is 2+2?")

    # Turn 1 assistant carries: text + tool_use + tool_result + text.
    ev1 = [m for m in msgs if m["role"] == "assistant"][0]["events"]
    assert len(ev1) == 4, f"turn1 events={len(ev1)}"
    # Turn 2 assistant carries one text event.
    ev2 = [m for m in msgs if m["role"] == "assistant"][1]["events"]
    assert len(ev2) == 1, f"turn2 events={len(ev2)}"

    # Registry recorded the import.
    assert sess.registry_key in native_import.already_imported_keys()
    return sess, root_id


def test_idempotent_reimport(sess, root_id):
    import session_store
    before = len(session_store.list_sessions())
    again = native_import.import_session(sess)
    assert again == root_id, "re-import should return the same root_id"
    after = len(session_store.list_sessions())
    assert before == after, "re-import must not create a new session"


def main():
    sess, root_id = test_claude_enumerate_and_import()
    test_idempotent_reimport(sess, root_id)
    print("OK: native_import claude enumerate + multi-turn ingest + idempotency")


if __name__ == "__main__":
    main()
