"""Provider-neutral native transcript parsing.

The provider-agnostic core shared by the BA session miners
(:mod:`native_session_miner`), the raw transcript search
(:mod:`native_session_prompt_search`), and the FTS index
(:mod:`native_transcript_index`): the :class:`NativeElement` /
:class:`NativeCandidate` shapes, the per-format message parsers and element
extractors (Claude / Codex / Gemini / Windsurf), and the provider-native root
discovery helpers.

This module deliberately has NO import-time dependency on the Better Agent
event stack (``session_miner`` / ``render_tree_hydrate`` / ``orchs``) or on
``paths`` — :meth:`NativeCandidate.parse` lazy-imports ``session_miner`` only
when a BA :class:`~session_miner.SessionVisit` is actually requested, and
``_claude_projects_roots`` lazy-imports ``config_store`` best-effort. That
keeps the module importable standalone (the transcript-search product vendors
it) while the backend keeps single-source-of-truth ownership.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass
class NativeElement:
    """One greppable unit from any provider transcript.

    The structural ``kind`` is assigned by the per-format extractor; it is the
    provider-neutral vocabulary the shared search + categorizer operate on:
    ``user_prompt`` | ``assistant_text`` | ``reasoning`` | ``tool_call`` |
    ``tool_result`` | ``command`` | ``meta``. ``tool_name`` is set for
    ``tool_call``/``tool_result`` so the categorizer can classify it (Bash vs
    Edit vs Read…) without re-parsing provider shapes.
    """

    kind: str
    role: str
    text: str
    tool_name: str = ""
    timestamp: str = ""
    id: str = ""


@dataclass
class NativeCandidate:
    """A discovered native transcript to parse — resolution done, parse pending.

    :meth:`parse` reads the transcript and builds the BA
    ``session_miner.SessionVisit``; it is the expensive step, kept separate
    from discovery so callers can run it concurrently. ``session_miner`` is
    imported lazily inside :meth:`parse` so this module stays importable
    without the BA event stack — standalone consumers use
    :meth:`parse_elements` only."""

    key: str
    sid: str
    cwd: str
    data: dict
    transcript: Path
    mtime: float
    format: str = "claude"

    def parse(self):
        from session_miner import SessionVisit
        try:
            if self.format == "codex":
                messages, events_by_msg_id = _codex_messages(self.transcript)
            elif self.format == "gemini":
                messages, events_by_msg_id = _gemini_messages(self.transcript)
            elif self.format == "pi":
                messages, events_by_msg_id = _pi_messages(self.transcript)
            elif self.format == "windsurf":
                messages, events_by_msg_id = _windsurf_messages(self.transcript)
            else:
                messages, events_by_msg_id = _native_messages(self.transcript)
        except (OSError, ValueError, InvalidTag):
            return None
        return SessionVisit(
            sid=self.sid,
            cwd=self.cwd,
            data=self.data,
            messages=messages,
            events_by_msg_id=events_by_msg_id,
        )

    def parse_elements(self) -> list[NativeElement]:
        """Full transcript element stream for the generalized grep — every
        greppable unit (prompts, replies, reasoning, tool calls, tool results,
        commands, meta), not just user/assistant text. Dispatches per format."""
        try:
            if self.format == "codex":
                return _codex_elements(self.transcript)
            if self.format == "gemini":
                return _gemini_elements(self.transcript)
            if self.format == "pi":
                return _pi_elements(self.transcript)
            if self.format == "windsurf":
                return _windsurf_elements(self.transcript)
            return _claude_elements(self.transcript)
        except (OSError, ValueError, InvalidTag):
            return []

# Claude CLI user lines that are injected context/commands, not typed prompts.
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

_WINDSURF_KEY = b"safeCodeiumworldKeYsecretBalloon"
_WINDSURF_NONCE_LEN = 12


def _decode_cwd_token(token: str) -> str:
    """Best-effort reverse of :func:`paths.encode_cwd` for a projects dir name.

    ``encode_cwd`` collapses ``/ \\ : _`` all to ``-``, so the reverse is
    ambiguous for paths containing underscores — callers that need an exact
    cwd match compare via ``encode_cwd`` rather than this string. Used only for
    display and as a fallback cwd when no BA record enriches a transcript.
    """
    if not token:
        return ""
    body = token.lstrip("-")
    if not body:
        return ""
    return "/" + body.replace("-", "/")


def _is_real_user_prompt(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.lower().startswith("caveat:"):
        return False
    return not stripped.startswith(_NON_PROMPT_TAGS)


def _user_text(content: object) -> str | None:
    """Extract a real typed-prompt string from a native user message, or None."""
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


def _has_edit_tool(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _TOOL_EDIT_NAMES
        for b in content
    )


def _native_messages(transcript_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """Parse a Claude-shaped transcript into (messages, events_by_msg_id).

    Works for both Claude's own ``projects/<cwd>/<sid>.jsonl`` and the
    Codex/Gemini run-dir ``session_events.jsonl`` (both are Claude message
    shaped). ``messages`` holds user/assistant turns in order; per assistant
    turn a single render-tree-shaped ``agent_message`` event carries the native
    content blocks — enough for ``extract_output_text`` (previous reply) and
    ``_edited_files_from_events`` (tool edits) without bespoke logic.
    """
    messages: list[dict] = []
    events_by_msg_id: dict[str, list[dict]] = {}
    with transcript_path.open(encoding="utf-8") as f:
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


# Codex user turns that are injected context, not typed prompts. Codex wraps the
# cwd/env in an <environment_context> input_text block and appends instructions
# under <user_instructions>; both look like a user turn but are CLI-injected.
_CODEX_NON_PROMPT_TAGS = ("<environment_context>", "<user_instructions>")


def _codex_is_real_prompt(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return not stripped.startswith(_CODEX_NON_PROMPT_TAGS)


def _codex_messages(transcript_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """Parse a Codex rollout transcript (``~/.codex/sessions/.../rollout-*.jsonl``).

    Each line is ``{"type", "timestamp", "payload"}``. User turns are
    ``response_item`` payloads with ``role=="user"`` whose content is a list of
    ``input_text`` blocks. CLI-injected ``<environment_context>`` /
    ``<user_instructions>`` blocks are dropped so only typed prompts remain.
    """
    messages: list[dict] = []
    events_by_msg_id: dict[str, list[dict]] = {}
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if obj.get("type") != "response_item" or payload.get("role") != "user":
                continue
            content = payload.get("content")
            texts: list[str] = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        texts.append(block["text"])
            text = "\n".join(t for t in texts if t).strip()
            if not _codex_is_real_prompt(text):
                continue
            uid = payload.get("id") if isinstance(payload.get("id"), str) else ""
            messages.append({"role": "user", "content": text, "timestamp": ts, "id": uid})
    return messages, events_by_msg_id


def _gemini_messages(transcript_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """Parse a Gemini chat-history transcript (``~/.gemini/tmp/<cwd>/chats/*.jsonl``).

    The first line is session metadata; subsequent lines are turns
    ``{"id", "timestamp", "type": "user"|"gemini", "content": [{"text"}]}``,
    interleaved with ``{"$set": ...}`` update lines (skipped). User turns become
    ``role=="user"`` messages; gemini turns become ``role=="assistant"``.
    """
    messages: list[dict] = []
    events_by_msg_id: dict[str, list[dict]] = {}
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            turn_type = obj.get("type")
            if turn_type not in ("user", "gemini"):
                continue
            content = obj.get("content")
            texts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        texts.append(block["text"])
            elif isinstance(content, str):
                texts.append(content)
            text = "\n".join(t for t in texts if t).strip()
            if not text:
                continue
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
            uid = obj.get("id") if isinstance(obj.get("id"), str) else ""
            role = "user" if turn_type == "user" else "assistant"
            messages.append({"role": role, "content": text, "timestamp": ts, "id": uid})
    return messages, events_by_msg_id


def _windsurf_messages(transcript_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    messages: list[dict] = []
    for element in _windsurf_elements(transcript_path):
        if element.kind not in {"user_prompt", "assistant_text"}:
            continue
        messages.append({
            "role": "user" if element.kind == "user_prompt" else "assistant",
            "content": element.text,
            "timestamp": element.timestamp,
            "id": element.id,
        })
    return messages, {}


def _pi_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _pi_message_entry(obj: dict) -> dict | None:
    if obj.get("type") == "message" and isinstance(obj.get("message"), dict):
        return obj["message"]
    if obj.get("type") == "custom_message":
        return {
            "role": "custom",
            "customType": obj.get("customType"),
            "content": obj.get("content"),
            "display": obj.get("display"),
            "timestamp": obj.get("timestamp"),
        }
    return None


def _pi_messages(transcript_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    messages: list[dict] = []
    events_by_msg_id: dict[str, list[dict]] = {}
    for obj in _pi_jsonl_objects(transcript_path):
        message = _pi_message_entry(obj)
        if not message:
            continue
        role = message.get("role")
        ts = _pi_timestamp(obj, message)
        uid = obj.get("id") if isinstance(obj.get("id"), str) else ""
        if role == "user":
            text = _pi_content_text(message.get("content")).strip()
            if text:
                messages.append({"role": "user", "content": text, "timestamp": ts, "id": uid})
        elif role == "assistant":
            content = message.get("content")
            text = _pi_assistant_text(content).strip()
            if text or _pi_has_tool_call(content):
                messages.append({"role": "assistant", "content": text, "timestamp": ts, "id": uid})
                if uid:
                    events_by_msg_id[uid] = [_pi_wrapped_event("assistant", content, uid)]
    return messages, events_by_msg_id


def _pi_jsonl_objects(transcript_path: Path) -> list[dict]:
    objects: list[dict] = []
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                objects.append(obj)
    return objects


def _pi_timestamp(obj: dict, message: dict | None = None) -> str:
    if message and isinstance(message.get("timestamp"), (int, float)):
        return datetime.fromtimestamp(message["timestamp"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    if message and isinstance(message.get("timestamp"), str):
        return message["timestamp"]
    return obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""


def _pi_assistant_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                parts.append(block["thinking"])
    return "\n".join(parts)


def _pi_has_tool_call(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "toolCall"
        for block in content
    )


def _pi_wrapped_event(role: str, content: object, uid: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": role,
            "uuid": uid,
            "message": {"role": role, "content": _pi_claude_content(role, content)},
        },
    }


def _pi_claude_content(role: str, content: object) -> object:
    if isinstance(content, str):
        return content if role == "user" else [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            out.append({"type": "text", "text": block["text"]})
        elif kind == "thinking" and isinstance(block.get("thinking"), str):
            out.append({"type": "thinking", "thinking": block["thinking"]})
        elif kind == "toolCall":
            out.append({
                "type": "tool_use",
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "input": block.get("arguments") if isinstance(block.get("arguments"), dict) else {},
            })
    return out


# ─── generalized element extractors ────────────────────────────────────────
# Per-format adapters that emit the provider-neutral NativeElement stream used
# by the generalized transcript grep + categorizer. They share the content /
# noise helpers above with the message parsers so there is one reading of each
# format's shapes. kinds: user_prompt | assistant_text | reasoning | tool_call
# | tool_result | command | meta.

_COMMAND_TAGS = ("<command-name>", "<bash-input>")


def _stringify(value: object) -> str:
    """Render a tool argument/output blob as greppable text."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _claude_user_kind(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(_COMMAND_TAGS):
        return "command"
    return "user_prompt" if _is_real_user_prompt(stripped) else "meta"


def _claude_elements(transcript_path: Path) -> list[NativeElement]:
    """Claude-shaped transcript → elements (covers ~/.claude/projects and the
    BA run-dir session_events.jsonl, both Claude message-shaped)."""
    elements: list[NativeElement] = []
    with transcript_path.open(encoding="utf-8") as f:
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
            line_type = obj.get("type")
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
            uid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else ""
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            role = message.get("role") if isinstance(message.get("role"), str) else ""
            if line_type == "user":
                if isinstance(content, str):
                    if content.strip():
                        elements.append(NativeElement(_claude_user_kind(content), role or "user", content.strip(), "", ts, uid))
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type")
                        if bt == "tool_result":
                            text = _tool_result_text(block.get("content")).strip()
                            if text:
                                elements.append(NativeElement("tool_result", "user", text, "", ts, block.get("tool_use_id") or uid))
                        elif bt == "text" and isinstance(block.get("text"), str):
                            text = block["text"].strip()
                            if text:
                                elements.append(NativeElement(_claude_user_kind(text), role or "user", text, "", ts, uid))
            elif line_type == "assistant":
                if isinstance(content, str):
                    if content.strip():
                        elements.append(NativeElement("assistant_text", "assistant", content.strip(), "", ts, uid))
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type")
                        if bt == "text" and isinstance(block.get("text"), str):
                            text = block["text"].strip()
                            if text:
                                elements.append(NativeElement("assistant_text", "assistant", text, "", ts, uid))
                        elif bt == "thinking" and isinstance(block.get("thinking"), str):
                            text = block["thinking"].strip()
                            if text:
                                elements.append(NativeElement("reasoning", "assistant", text, "", ts, uid))
                        elif bt == "tool_use":
                            name = block.get("name") if isinstance(block.get("name"), str) else ""
                            text = f"{name} {_stringify(block.get('input'))}".strip()
                            elements.append(NativeElement("tool_call", "assistant", text, name, ts, block.get("id") or uid))
    return elements


def _pi_elements(transcript_path: Path) -> list[NativeElement]:
    elements: list[NativeElement] = []
    for obj in _pi_jsonl_objects(transcript_path):
        uid = obj.get("id") if isinstance(obj.get("id"), str) else ""
        if obj.get("type") in {"compaction", "branch_summary"}:
            summary = obj.get("summary") if isinstance(obj.get("summary"), str) else ""
            if summary.strip():
                elements.append(NativeElement("reasoning", "assistant", summary.strip(), "", _pi_timestamp(obj), uid))
            continue
        message = _pi_message_entry(obj)
        if not message:
            continue
        role = message.get("role") if isinstance(message.get("role"), str) else ""
        ts = _pi_timestamp(obj, message)
        if role == "user":
            text = _pi_content_text(message.get("content")).strip()
            if text:
                elements.append(NativeElement("user_prompt", "user", text, "", ts, uid))
        elif role == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                elements.append(NativeElement("assistant_text", "assistant", content.strip(), "", ts, uid))
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    elements.append(NativeElement("assistant_text", "assistant", block["text"].strip(), "", ts, uid))
                elif kind == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"].strip():
                    elements.append(NativeElement("reasoning", "assistant", block["thinking"].strip(), "", ts, uid))
                elif kind == "toolCall":
                    name = block.get("name") if isinstance(block.get("name"), str) else ""
                    text = f"{name} {_stringify(block.get('arguments'))}".strip()
                    elements.append(NativeElement("tool_call", "assistant", text, name, ts, block.get("id") or uid))
        elif role == "toolResult":
            text = _pi_content_text(message.get("content")).strip()
            if text:
                name = message.get("toolName") if isinstance(message.get("toolName"), str) else ""
                elements.append(NativeElement("tool_result", "user", text, name, ts, message.get("toolCallId") or uid))
        elif role == "bashExecution":
            command = message.get("command") if isinstance(message.get("command"), str) else ""
            if command.strip():
                elements.append(NativeElement("command", "user", command.strip(), "bash", ts, uid))
            output = message.get("output") if isinstance(message.get("output"), str) else ""
            if output.strip():
                elements.append(NativeElement("tool_result", "user", output.strip(), "bash", ts, uid))
        elif role in {"custom", "branchSummary", "compactionSummary"}:
            text = _pi_content_text(message.get("content")).strip()
            if not text:
                text = str(message.get("summary") or "").strip()
            if text:
                elements.append(NativeElement("meta", role, text, "", ts, uid))
    return elements


def _codex_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text") if isinstance(block.get("text"), str) else None
                if txt is None:
                    txt = block.get("content") if isinstance(block.get("content"), str) else None
                if txt:
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def _codex_output_text(output: object) -> str:
    """Codex function_call_output payloads wrap the text as a JSON string
    (``{"output": "..."}``) — unwrap when present, else return as-is."""
    if isinstance(output, str):
        try:
            unwrapped = json.loads(output)
            if isinstance(unwrapped, dict) and isinstance(unwrapped.get("output"), str):
                return unwrapped["output"]
        except json.JSONDecodeError:
            pass
        return output
    return _stringify(output)


def _codex_elements(transcript_path: Path) -> list[NativeElement]:
    elements: list[NativeElement] = []
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            pt = payload.get("type")
            uid = payload.get("id") if isinstance(payload.get("id"), str) else ""
            if pt == "message":
                role = payload.get("role") if isinstance(payload.get("role"), str) else ""
                text = _codex_content_text(payload.get("content")).strip()
                if not text:
                    continue
                if role == "user":
                    kind = "user_prompt" if _codex_is_real_prompt(text) else "meta"
                    elements.append(NativeElement(kind, "user", text, "", ts, uid))
                elif role == "assistant":
                    elements.append(NativeElement("assistant_text", "assistant", text, "", ts, uid))
            elif pt in ("agent_reasoning", "reasoning"):
                text = _codex_content_text(payload.get("content")).strip()
                if text:
                    elements.append(NativeElement("reasoning", "assistant", text, "", ts, uid))
            elif pt in ("function_call", "custom_tool_call"):
                name = payload.get("name") if isinstance(payload.get("name"), str) else ""
                args = payload.get("arguments")
                if args is None:
                    args = payload.get("input")
                text = f"{name} {_stringify(args)}".strip()
                elements.append(NativeElement("tool_call", "assistant", text, name, ts, uid))
            elif pt in ("function_call_output", "custom_tool_call_output"):
                text = _codex_output_text(payload.get("output")).strip()
                if text:
                    elements.append(NativeElement("tool_result", "user", text, "", ts, uid))
    return elements


def _gemini_elements(transcript_path: Path) -> list[NativeElement]:
    elements: list[NativeElement] = []
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            turn_type = obj.get("type")
            if turn_type not in ("user", "gemini"):
                continue
            ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
            uid = obj.get("id") if isinstance(obj.get("id"), str) else ""
            role = "user" if turn_type == "user" else "assistant"
            text_kind = "user_prompt" if turn_type == "user" else "assistant_text"
            content = obj.get("content")
            if isinstance(content, str):
                if content.strip():
                    elements.append(NativeElement(text_kind, role, content.strip(), "", ts, uid))
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str) and block["text"].strip():
                    elements.append(NativeElement(text_kind, role, block["text"].strip(), "", ts, uid))
                if isinstance(block.get("functionCall"), dict):
                    fc = block["functionCall"]
                    name = fc.get("name") if isinstance(fc.get("name"), str) else ""
                    elements.append(NativeElement("tool_call", role, f"{name} {_stringify(fc.get('args'))}".strip(), name, ts, uid))
                if isinstance(block.get("functionResponse"), dict):
                    fr = block["functionResponse"]
                    name = fr.get("name") if isinstance(fr.get("name"), str) else ""
                    text = _stringify(fr.get("response")).strip()
                    if text:
                        elements.append(NativeElement("tool_result", role, text, name, ts, uid))
    return elements


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if index >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7


def _parse_pb(data: bytes, index: int = 0, end: int | None = None) -> list[tuple[int, int, object]]:
    if end is None:
        end = len(data)
    out: list[tuple[int, int, object]] = []
    while index < end:
        tag, index = _read_varint(data, index)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, index = _read_varint(data, index)
            out.append((field, wire_type, value))
            continue
        if wire_type == 1:
            if index + 8 > end:
                raise ValueError("truncated fixed64 protobuf field")
            out.append((field, wire_type, data[index:index + 8]))
            index += 8
            continue
        if wire_type == 2:
            length, index = _read_varint(data, index)
            if index + length > end:
                raise ValueError("truncated length-delimited protobuf field")
            out.append((field, wire_type, data[index:index + length]))
            index += length
            continue
        if wire_type == 5:
            if index + 4 > end:
                raise ValueError("truncated fixed32 protobuf field")
            out.append((field, wire_type, data[index:index + 4]))
            index += 4
            continue
        raise ValueError(f"unsupported protobuf wire type {wire_type}")
    return out


def _first_fields(data: bytes) -> dict[int, object]:
    fields: dict[int, object] = {}
    for field, _wire_type, value in _parse_pb(data):
        fields.setdefault(field, value)
    return fields


def _pb_string(value: object) -> str | None:
    if not isinstance(value, bytes):
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _windsurf_timestamp(meta: object) -> str:
    if not isinstance(meta, bytes):
        return ""
    ts = _first_fields(meta).get(1)
    if not isinstance(ts, bytes):
        return ""
    seconds = _first_fields(ts).get(1)
    if not isinstance(seconds, int):
        return ""
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _windsurf_user_prompt(step_fields: dict[int, object]) -> str | None:
    body = step_fields.get(19)
    if not isinstance(body, bytes):
        return None
    fields = _first_fields(body)
    text = _pb_string(fields.get(2))
    if text is None and isinstance(fields.get(3), bytes):
        text = _pb_string(_first_fields(fields[3]).get(1))
    return text


def _windsurf_tool_proposal(step_fields: dict[int, object]) -> tuple[str | None, dict[str, str | None] | None]:
    body = step_fields.get(20)
    if not isinstance(body, bytes):
        return None, None
    fields = _first_fields(body)
    text = _pb_string(fields.get(1)) or _pb_string(fields.get(8))
    tool = None
    if isinstance(fields.get(7), bytes):
        tool_fields = _first_fields(fields[7])
        tool = {
            "id": _pb_string(tool_fields.get(1)),
            "name": _pb_string(tool_fields.get(2)),
            "args": _pb_string(tool_fields.get(3)),
        }
    return text, tool


def _windsurf_plan_text(step_fields: dict[int, object]) -> str | None:
    body = step_fields.get(30)
    if not isinstance(body, bytes):
        return None
    return _pb_string(_first_fields(body).get(4))


def _windsurf_code_edit_tool(step_fields: dict[int, object]) -> str:
    body = step_fields.get(10)
    if not isinstance(body, bytes):
        return ""
    fields = _first_fields(body)
    title = None
    uri = None
    if isinstance(fields.get(1), bytes):
        title_root = _first_fields(fields[1])
        if isinstance(title_root.get(1), bytes):
            title = _pb_string(_first_fields(title_root[1]).get(1))
    if isinstance(fields.get(2), bytes):
        uri_root = _first_fields(fields[2])
        if isinstance(uri_root.get(1), bytes):
            uri = _pb_string(_first_fields(uri_root[1]).get(8))
    return uri or title or ""


def _windsurf_grep_tool(step_fields: dict[int, object]) -> str:
    body = step_fields.get(13)
    if not isinstance(body, bytes):
        return ""
    fields = _first_fields(body)
    return f"pattern={_pb_string(fields.get(1))!r} glob={_pb_string(fields.get(2))!r}"


def _windsurf_decrypt(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) <= _WINDSURF_NONCE_LEN:
        raise ValueError("windsurf protobuf is too short")
    return AESGCM(_WINDSURF_KEY).decrypt(data[:_WINDSURF_NONCE_LEN], data[_WINDSURF_NONCE_LEN:], None)


def _windsurf_elements(transcript_path: Path) -> list[NativeElement]:
    elements: list[NativeElement] = []
    for field, wire_type, step_data in _parse_pb(_windsurf_decrypt(transcript_path)):
        if field != 2 or wire_type != 2 or not isinstance(step_data, bytes):
            continue
        step_fields: dict[int, object] = {}
        for step_field, _step_wire_type, value in _parse_pb(step_data):
            step_fields.setdefault(step_field, value)
        step_id = str(step_fields.get(1) or "")
        timestamp = _windsurf_timestamp(step_fields.get(5))
        if 19 in step_fields:
            text = _windsurf_user_prompt(step_fields)
            if text:
                elements.append(NativeElement("user_prompt", "user", text, "", timestamp, step_id))
        elif 20 in step_fields:
            text, tool = _windsurf_tool_proposal(step_fields)
            if text:
                elements.append(NativeElement("assistant_text", "assistant", text, "", timestamp, step_id))
            if tool and tool.get("args"):
                elements.append(NativeElement(
                    "tool_call", "assistant", tool["args"] or "",
                    tool.get("name") or "", timestamp, tool.get("id") or step_id,
                ))
        elif 30 in step_fields:
            text = _windsurf_plan_text(step_fields)
            if text:
                elements.append(NativeElement("assistant_text", "assistant", text, "", timestamp, step_id))
        elif 10 in step_fields:
            text = _windsurf_code_edit_tool(step_fields)
            if text:
                elements.append(NativeElement("tool_call", "assistant", text, "edit_file", timestamp, step_id))
        elif 13 in step_fields:
            text = _windsurf_grep_tool(step_fields)
            if text:
                elements.append(NativeElement("tool_call", "assistant", text, "grep_search", timestamp, step_id))
    return elements


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ─── provider-native root discovery ─────────────────────────────────────────

def _claude_projects_roots() -> list[Path]:
    """Every claude projects root on disk across all provider configurations.

    Claude-compatible providers may each set their own ``config_dir``
    (e.g. ``~/.claude-zai`` for a Z.AI claude provider), and the claude CLI
    writes ``<config_dir>/projects``. To cover them all we union: every
    provider's ``config_dir``, the ``CLAUDE_CONFIG_DIR`` env var, and every
    ``~/.claude*`` dir that actually has a ``projects/`` subdir.
    """
    from paths import resolve_claude_config_dir
    roots: set[Path] = set()
    try:
        import config_store
        for prov in config_store.list_providers().get("providers", []):
            if not isinstance(prov, dict) or prov.get("kind") != "claude":
                continue
            cfg_dir = (prov.get("config_dir") or "").strip()
            if cfg_dir:
                roots.add(resolve_claude_config_dir(cfg_dir) / "projects")
    except Exception:
        pass
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if env_dir:
        roots.add(resolve_claude_config_dir(env_dir) / "projects")
    try:
        for entry in Path.home().iterdir():
            if entry.is_dir() and entry.name.startswith(".claude") and (entry / "projects").is_dir():
                roots.add(entry / "projects")
    except OSError:
        pass
    return sorted(r for r in roots if r.exists())


def _codex_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _gemini_chats_root() -> Path:
    return Path.home() / ".gemini" / "tmp"


def _pi_sessions_root() -> Path:
    raw_session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "")
    if raw_session_dir:
        return Path(os.path.expanduser(os.path.expandvars(raw_session_dir)))
    raw_agent_dir = os.environ.get("PI_CODING_AGENT_DIR", "")
    if raw_agent_dir:
        return Path(os.path.expanduser(os.path.expandvars(raw_agent_dir))) / "sessions"
    return Path.home() / ".pi" / "agent" / "sessions"


def _windsurf_cascade_roots() -> list[Path]:
    base = Path.home() / ".codeium"
    return [
        root
        for root in (base / "cascade", base / "windsurf" / "cascade")
        if root.exists()
    ]


def _codex_first_cwd(transcript: Path) -> str:
    """cwd from a codex rollout's opening ``session_meta`` line, or ""."""
    try:
        with transcript.open(encoding="utf-8") as f:
            first = f.readline()
        obj = json.loads(first)
        payload = obj.get("payload") if isinstance(obj, dict) else None
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        return cwd if isinstance(cwd, str) else ""
    except (OSError, json.JSONDecodeError):
        return ""
