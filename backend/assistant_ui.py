"""Assistant extension core substrate.

The assistant is a single, persistent, **reused native session** the user talks
1-on-1 with. Its optimized prompt + stateless board preamble are delivered via
the session's `capability_contexts` — the existing per-session, per-turn-replayed
system-prompt-append path (no new per-session prompt field, no provider surgery).

This module owns the find-or-create singleton, search (reuses the ask search
worker), delegation (reuses session_bridge), and last-turn extraction. The
board-update classify/rank fork lives elsewhere (TBD); this is the routing tier.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import extension_store
import paths
import session_bridge
import session_manager
import session_search

_LOCK = threading.Lock()


def _ext_id() -> str | None:
    return extension_store.BUILTIN_ASSISTANT_EXTENSION_ID


def _state_path() -> Path:
    return paths.ba_home() / "assistant_singleton.json"


def _install_path() -> Path | None:
    eid = _ext_id()
    if not eid:
        return None
    record = extension_store.get_extension(eid) or {}
    install = (record.get("source") or {}).get("install_path")
    return Path(install).expanduser() if install else None


def _system_prompt() -> str:
    path = (_install_path() or Path(".")) / "prompts" / "system.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_capability_contexts(board_preamble: str = "") -> list[dict]:
    """Capability context appended to the assistant session's system prompt every
    turn. v1: the role prompt; `board_preamble` (stateless item set) is appended
    here once the board mechanism feeds it. State is deliberately NOT included —
    it lives in the volatile tail to keep this cached region byte-stable."""
    content = _system_prompt()
    if board_preamble:
        content = f"{content}\n\n{board_preamble}" if content else board_preamble
    if not content.strip():
        return []
    return [{"name": "Assistant", "category": "role", "content": content}]


def _read_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def ensure_singleton() -> dict:
    """Find-or-create the persistent assistant native session and refresh its
    capability_contexts so prompt/preamble edits take effect idempotently.
    Returns the live session record."""
    with _LOCK:
        eid = _ext_id()
        if not eid:
            raise RuntimeError("assistant extension id not loaded (private registry absent)")
        sid = _read_state().get("session_id")
        sess = session_manager.get(sid) if sid else None
        caps = build_capability_contexts()
        if sess is None:
            sess = session_manager.create(
                name="Assistant",
                orchestration_mode="native",
                capability_contexts=caps,
            )
            _write_state({"session_id": sess["id"]})
        elif caps:
            session_manager.set_capability_contexts(sess["id"], caps)
        return sess


def _msg_text(message: dict | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def last_turn(sid: str) -> dict:
    """Compact last-turn view of a session: the user prompt + the assistant's
    reply (the 'next/successor' message) + cwd. Used by the post-turn hook to
    feed the board-update fork without hauling the whole transcript."""
    sess = session_manager.get(sid) or {}
    messages = sess.get("messages") or []
    last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    assistant = last_assistant or {}
    return {
        "turn_id": assistant.get("id") or sid,
        "ts": assistant.get("ts"),
        "user_prompt": _msg_text(last_user),
        "assistant_message": _msg_text(last_assistant),
        "cwd": sess.get("cwd"),
        "delegated_to": None,
    }


async def search(query: str, *, max_results: int = 10, timeout: float = 120.0) -> dict:
    """Rank candidate target sessions for a prompt (reuses the ask provisioned
    search worker). Hint-augmentation (comment + source-session map) is a
    follow-up layered on the query."""
    return await session_search.run_search_sessions_session(
        query, max_results=max_results, timeout=timeout
    )


async def delegate(target_sid: str, prompt: str) -> dict:
    """Send a prompt to a target session and run its turn; returns the
    session_bridge result (final assistant message + metadata). The target does
    the work in the background; the caller does not block on the UI thread."""
    return await session_bridge.run_for_extension(target_sid, prompt, source="assistant")
