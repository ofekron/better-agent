"""Native provider-transcript session miner.

:class:`NativeSessionMiner` is the native-source counterpart to
:class:`session_miner.SessionMiner`. Both subclass :class:`SessionMinerBase`
and yield the same :class:`SessionVisit`, so every consumer
(:class:`SessionConsumer`) works against either source unchanged. The
difference is the message source: BA session snapshots vs the provider CLI's
own transcript.

Discovery uses the Better Agent session record ONLY as an index — it gives the
reliable ``cwd``, the provider, and the ``agent_session_id`` that links to the
native transcript. The prompt CONTENT is read from the native transcript, which
is the raw ground truth (the BA render tree is a projection that can have gaps
after crashes/recovery). This is Claude-first: a session resolves to a native
transcript only when ``<claude projects root>/<agent_session_id>.jsonl`` exists,
so non-Claude providers are skipped until their native readers land.

A native user line is kept only when it is a REAL typed prompt: ``type=="user"``
with text content, rejecting tool-result turns, sidechain/meta turns, and the
CLI's command/stdout/system wrappers. Assistant turns are bridged into the
render-tree event shape so the shared consumer helpers
(``extract_output_text`` for the previous reply, ``_edited_files_from_events``
for edited files) work without modification.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from paths import claude_projects_root_for_session, encode_cwd
from session_miner import SessionMinerBase, SessionVisit, sessions_dir

# Claude CLI user lines that are injected context/commands, not typed prompts.
# Matched against the stripped content's leading tag.
_NON_PROMPT_TAGS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-rc>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<bash-rc>",
    "<system-reminder>",
    "<system-warning>",
    "<user-byte>",
    "<user-memory-warning>",
    "<command-cancellation>",
    "<task-summary>",
    "<request-limit-hit>",
    "<caveat>",
)

_TOOL_EDIT_NAMES = {"Edit", "MultiEdit", "Write", "replace", "write_file"}


def _is_real_user_prompt(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if lower.startswith("caveat:"):
        return False
    return not stripped.startswith(_NON_PROMPT_TAGS)


def _user_text(content: object) -> str | None:
    """Extract a real typed-prompt string from a native user message, or None.

    Rejects tool-result turns and turns whose only text is a CLI wrapper.
    """
    if isinstance(content, str):
        return content if _is_real_user_prompt(content) else None
    if not isinstance(content, list):
        return None
    has_tool_result = False
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_result":
            has_tool_result = True
        elif kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    if has_tool_result or not texts:
        return None
    joined = "\n".join(texts)
    return joined if _is_real_user_prompt(joined) else None


def _assistant_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _native_messages(native_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """Parse a native Claude transcript into (messages, events_by_msg_id).

    ``messages`` holds the user/assistant turns in order (for role-based
    iteration and timestamp derivation). ``events_by_msg_id`` holds, per
    assistant turn, a single render-tree-shaped ``agent_message`` event carrying
    the native content blocks — enough for ``extract_output_text`` (previous
    reply) and ``_edited_files_from_events`` (tool edits) without bespoke logic.
    """
    messages: list[dict] = []
    events_by_msg_id: dict[str, list[dict]] = {}
    with native_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain") or obj.get("isMeta"):
                continue
            kind = obj.get("type")
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
            uid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else ""
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            if kind == "user":
                text = _user_text(message.get("content"))
                if text is None:
                    continue
                messages.append({"role": "user", "content": text, "timestamp": ts, "id": uid})
            elif kind == "assistant":
                content = message.get("content")
                text = _assistant_text(content)
                if not text and not _has_edit_tool(content):
                    continue
                messages.append({"role": "assistant", "content": text, "timestamp": ts, "id": uid})
                if uid:
                    events_by_msg_id[uid] = [{
                        "type": "agent_message",
                        "data": {
                            "type": "assistant",
                            "uuid": uid,
                            "message": {"role": "assistant", "content": content if isinstance(content, list) else text},
                        },
                    }]
    return messages, events_by_msg_id


def _has_edit_tool(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _TOOL_EDIT_NAMES
        for b in content
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class NativeSessionMiner(SessionMinerBase):
    """Provider-native transcript source (Claude-first).

    Discovery iterates Better Agent session records (``sessions/*.json``) for
    the reliable ``cwd`` and ``agent_session_id``; content is read from the
    linked native Claude transcript. The watermark key is the BA session-json
    filename (so it shares state shape with :class:`SessionMiner`) and the
    fingerprint is the max of the BA record and native transcript mtimes, so a
    change to either triggers a re-mine.
    """

    def __init__(self, state: dict, *, root: Path | None = None) -> None:
        super().__init__(state, root=root or sessions_dir())

    def _iter_sources(self) -> Iterable[tuple[str, SessionVisit, float]]:
        if not self._root.exists():
            return
        for session_json in sorted(self._root.glob("*.json")):
            if session_json.name.endswith(".summary.json"):
                continue
            try:
                data = json.loads(session_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            agent_session_id = data.get("agent_session_id")
            if not isinstance(agent_session_id, str) or not agent_session_id:
                continue
            cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
            if not cwd:
                continue
            native_path = (
                claude_projects_root_for_session(data)
                / encode_cwd(cwd)
                / f"{agent_session_id}.jsonl"
            )
            if not native_path.exists():
                continue  # non-Claude provider or transcript not present yet
            try:
                messages, events_by_msg_id = _native_messages(native_path)
            except OSError:
                continue
            sid = session_json.stem
            visit = SessionVisit(
                sid=sid,
                cwd=cwd,
                data=data,
                messages=messages,
                events_by_msg_id=events_by_msg_id,
            )
            yield session_json.name, visit, max(_mtime(session_json), _mtime(native_path))
