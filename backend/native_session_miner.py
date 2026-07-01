"""Provider-native transcript session miners.

Four native :class:`SessionMinerBase` implementations, one per supported
provider, plus the shared parser. They are the native-source counterparts to
:class:`session_miner.SessionMiner` (the Better Agent snapshot source);
together that is five SessionMiner implementations of the one abstraction.

- :class:`NativeClaudeSessionMiner` — Claude ``projects/<cwd>/<sid>.jsonl``.
- :class:`NativeCodexSessionMiner` — Codex run-dir ``session_events.jsonl``
  (Claude-shaped, captured by the runner from the Codex stream).
- :class:`NativeGeminiSessionMiner` — Gemini run-dir ``session_events.jsonl``
  (Claude-shaped, pre-normalized by ``runner_gemini``).
- :class:`NativeBetterAgentSessionMiner` — Better Agent's own runner
  (``runner_better_agent``, ``openai`` provider kind) run-dir
  ``session_events.jsonl`` (Claude-shaped).

Discovery uses the Better Agent session record only as an index — it gives the
reliable ``cwd``, the ``provider_id`` (→ provider kind), and the
``agent_session_id`` that links to the native transcript. Prompt CONTENT is read
from the native transcript, the raw ground truth (the BA render tree is a
projection that can have gaps after crashes/recovery).

Codex and Gemini write Claude-shaped events, so all three miners share one
parser (:func:`_native_messages`). A native user line is kept only when it is a
REAL typed prompt: ``type=="user"`` with text content, rejecting tool-result
turns, sidechain/meta turns, and the CLI's command/stdout wrappers. Assistant
turns are bridged into the render-tree event shape so the shared consumer
helpers (``extract_output_text`` for the previous reply,
``_edited_files_from_events`` for edited files) work without modification.

Each miner namespaces its watermark key (``claude:``/``codex:``/``gemini:``) so
they can share one persisted state dict with the BA snapshot miner without
collision.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from paths import bc_home, claude_projects_root_for_session, encode_cwd
from session_miner import SessionMinerBase, SessionVisit, sessions_dir


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

    :meth:`parse` reads the transcript and builds the :class:`SessionVisit`; it
    is the expensive step, kept separate from discovery so callers can run it
    concurrently."""

    key: str
    sid: str
    cwd: str
    data: dict
    transcript: Path
    mtime: float
    format: str = "claude"

    def parse(self) -> SessionVisit | None:
        try:
            if self.format == "codex":
                messages, events_by_msg_id = _codex_messages(self.transcript)
            elif self.format == "gemini":
                messages, events_by_msg_id = _gemini_messages(self.transcript)
            else:
                messages, events_by_msg_id = _native_messages(self.transcript)
        except OSError:
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
            return _claude_elements(self.transcript)
        except OSError:
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

_RUNS_DIR_NAME = "runs"


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


def _ba_session_cwd(sid: str) -> str:
    """Real cwd for a BA app session id, or "" when no record exists.

    Filesystem-first discovery has no BA index, so cwd is recovered from the
    session record when present (the authoritative source) and falls back to
    the encoded dir name otherwise.
    """
    try:
        rec = json.loads((sessions_dir() / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    cwd = rec.get("cwd")
    return cwd if isinstance(cwd, str) else ""


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


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _provider_kind(data: dict) -> str:
    """Resolve a BA session record's provider kind (claude/codex/gemini).

    Defaults to ``claude`` when the provider is missing or unconfigured, mirroring
    the backend's own default (``config_store`` treats unknown/missing kind as
    claude).
    """
    provider_id = data.get("provider_id")
    if isinstance(provider_id, str) and provider_id:
        try:
            import config_store
            rec = config_store.get_provider(provider_id)
        except Exception:
            rec = None
        if isinstance(rec, dict):
            kind = rec.get("kind")
            if isinstance(kind, str) and kind:
                return kind
    return "claude"


# Codex/Gemini run-dir index: {app_session_id -> run_dir}. Built on demand and
# refreshed when the runs dir mtime changes, so a long-lived process does not
# rescan thousands of run dirs every mining pass.
_RUN_INDEX: dict[str, Path] | None = None
_RUN_INDEX_MTIME: float = 0.0


def _runs_root() -> Path:
    return bc_home() / _RUNS_DIR_NAME


def _run_index() -> dict[str, Path]:
    global _RUN_INDEX, _RUN_INDEX_MTIME
    root = _runs_root()
    try:
        root_mtime = root.stat().st_mtime
    except OSError:
        return {}
    if _RUN_INDEX is not None and root_mtime == _RUN_INDEX_MTIME:
        return _RUN_INDEX
    try:
        from runs_dir import run_dirs_by_app_session
        index = run_dirs_by_app_session(root)
    except Exception:
        index = {}
    if index:
        _RUN_INDEX = index
        _RUN_INDEX_MTIME = root_mtime
        return index
    index = {}
    for state_path in root.glob("*/state.json"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        aid = data.get("app_session_id")
        if isinstance(aid, str) and aid:
            index[aid] = state_path.parent
    _RUN_INDEX = index
    _RUN_INDEX_MTIME = root_mtime
    return index


def _iter_ba_session_records(root: Path) -> Iterable[tuple[str, dict]]:
    """Yield (session_filename, data) for every non-summary BA session record."""
    if not root.exists():
        return
    for session_json in sorted(root.glob("*.json")):
        if session_json.name.endswith(".summary.json"):
            continue
        try:
            data = json.loads(session_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            yield session_json.name, data


def _claude_projects_roots() -> list[Path]:
    """Every claude projects root on disk across all provider configurations.

    Claude-compatible providers may each set their own ``config_dir``
    (e.g. ``~/.claude-zai`` for a Z.AI claude provider), and the claude CLI
    writes ``<config_dir>/projects``. To cover them all we union: every
    provider's ``config_dir``, the ``CLAUDE_CONFIG_DIR`` env var, and every
    ``~/.claude*`` dir that actually has a ``projects/`` subdir.
    """
    roots: set[Path] = set()
    try:
        import config_store
        for prov in config_store.list_providers().get("providers", []):
            if not isinstance(prov, dict) or prov.get("kind") != "claude":
                continue
            cfg_dir = (prov.get("config_dir") or "").strip()
            if cfg_dir:
                roots.add(Path(os.path.expanduser(os.path.expandvars(cfg_dir))) / "projects")
    except Exception:
        pass
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if env_dir:
        roots.add(Path(os.path.expanduser(os.path.expandvars(env_dir))) / "projects")
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


def iter_all_native_candidates() -> Iterable[NativeCandidate]:
    """Filesystem-first discovery of EVERY native transcript across ALL
    providers, ignoring the Better-Agent session index.

    The BA-indexed miners only see transcripts linked from a BA session record,
    which misses the bulk of raw native sessions — direct CLI usage and
    extension-spawned workers leave no BA record. This walker enumerates every
    provider's native store directly so the raw-grep search covers the whole
    corpus:

    - Claude (every config): all ``<config_dir>/projects/<encoded-cwd>/*.jsonl``
      — covers ``~/.claude``, ``~/.claude-zai``, and any provider ``config_dir``.
    - Codex native: ``~/.codex/sessions/**/*.jsonl`` rollout files.
    - Gemini native: ``~/.gemini/tmp/<cwd>/chats/session-*.jsonl``.
    - BA run-dirs: ``<runs>/<run_id>/session_events.jsonl`` (codex/gemini/ba-runner
      streams BA captured; Claude-shaped, so parsed as claude).
    """
    # Claude — every provider config root.
    for projects_root in _claude_projects_roots():
        if not projects_root.exists():
            continue
        for encoded_dir in projects_root.iterdir():
            if not encoded_dir.is_dir():
                continue
            decoded_cwd = _decode_cwd_token(encoded_dir.name)
            for transcript in encoded_dir.glob("*.jsonl"):
                yield NativeCandidate(
                    key=f"claude-fs:{projects_root.name}/{encoded_dir.name}/{transcript.stem}",
                    sid=transcript.stem,
                    cwd=decoded_cwd,
                    data={},
                    transcript=transcript,
                    mtime=_mtime(transcript),
                    format="claude",
                )

    # Codex native rollout store.
    codex_root = _codex_sessions_root()
    if codex_root.exists():
        for transcript in codex_root.rglob("*.jsonl"):
            yield NativeCandidate(
                key=f"codex-fs:{transcript.name}",
                sid=transcript.stem,
                cwd=_codex_first_cwd(transcript),
                data={},
                transcript=transcript,
                mtime=_mtime(transcript),
                format="codex",
            )

    # Gemini native chat store.
    gemini_root = _gemini_chats_root()
    if gemini_root.exists():
        for transcript in gemini_root.rglob("chats/session-*.jsonl"):
            cwd_dir = transcript.parent.parent
            yield NativeCandidate(
                key=f"gemini-fs:{cwd_dir.name}/{transcript.name}",
                sid=transcript.stem,
                cwd=_decode_cwd_token(cwd_dir.name),
                data={},
                transcript=transcript,
                mtime=_mtime(transcript),
                format="gemini",
            )

    # BA run-dirs (codex/gemini/ba-runner) — every run, regardless of BA linkage.
    for state_path in _runs_root().glob("*/state.json"):
        run_dir = state_path.parent
        transcript = run_dir / "session_events.jsonl"
        if not transcript.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        sid = state.get("app_session_id") if isinstance(state.get("app_session_id"), str) else run_dir.name
        yield NativeCandidate(
            key=f"run-fs:{run_dir.name}",
            sid=sid,
            cwd=_ba_session_cwd(sid),
            data={},
            transcript=transcript,
            mtime=_mtime(transcript),
            format="claude",
        )


class _NativeMinerBase(SessionMinerBase):
    """Shared discovery + parsing for native miners.

    Subclasses set :attr:`_kind` (the provider kind to mine) and
    :attr:`_key_prefix` (the watermark namespace). :meth:`_resolve_transcript`
    resolves the native transcript path for a BA session record (or None).
    """

    _kind: str = ""
    _key_prefix: str = ""

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        raise NotImplementedError

    def iter_candidates(self) -> Iterable["NativeCandidate"]:
        """Cheap discovery: yield one :class:`NativeCandidate` per transcript
        to parse, WITHOUT reading/parsing it. Splitting discovery from parsing
        lets a caller fan the (expensive) parse out across a thread pool."""
        for session_name, data in _iter_ba_session_records(self._root):
            if _provider_kind(data) != self._kind:
                continue
            sid = Path(session_name).stem
            transcript = self._resolve_transcript(data, sid)
            if transcript is None or not transcript.exists():
                continue
            source_mtime = max(_mtime(self._root / session_name), _mtime(transcript))
            yield NativeCandidate(
                key=f"{self._key_prefix}{session_name}",
                sid=sid,
                cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else "",
                data=data,
                transcript=transcript,
                mtime=source_mtime,
            )

    def _iter_sources(self) -> Iterable[tuple[str, SessionVisit, float]]:
        for candidate in self.iter_candidates():
            if not self.source_changed(candidate.key, candidate.mtime):
                self.scanned_count += 1
                continue
            visit = candidate.parse()
            if visit is None:
                continue
            yield candidate.key, visit, candidate.mtime


class NativeClaudeSessionMiner(_NativeMinerBase):
    """Claude native transcript source (``projects/<cwd>/<sid>.jsonl``)."""

    _kind = "claude"
    _key_prefix = "claude:"

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        agent_session_id = data.get("agent_session_id")
        cwd = data.get("cwd")
        if not isinstance(agent_session_id, str) or not agent_session_id:
            return None
        if not isinstance(cwd, str) or not cwd:
            return None
        return claude_projects_root_for_session(data) / encode_cwd(cwd) / f"{agent_session_id}.jsonl"


class _RunDirNativeMiner(_NativeMinerBase):
    """Codex/Gemini native source: run-dir ``session_events.jsonl``.

    Both providers write Claude-shaped events captured by their runners into a
    per-run dir; the run dir is resolved from the BA app session id (the session
    record's filename stem) via the run-dir index (``state.json.app_session_id``).
    """

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        run_dir = _run_index().get(sid)
        if run_dir is None:
            return None
        return run_dir / "session_events.jsonl"


class NativeCodexSessionMiner(_RunDirNativeMiner):
    _kind = "codex"
    _key_prefix = "codex:"


class NativeGeminiSessionMiner(_RunDirNativeMiner):
    _kind = "gemini"
    _key_prefix = "gemini:"


class NativeBetterAgentSessionMiner(_RunDirNativeMiner):
    """Better Agent's own runner source (``runner_better_agent``).

    The ``openai`` provider kind runs through ``runner_better_agent``, which —
    like Codex/Gemini — writes Claude-shaped ``session_events.jsonl`` into a
    per-run dir. Same run-dir mechanism, distinct kind/key so it is mined as its
    own source alongside the provider-native CLIs.
    """

    _kind = "openai"
    _key_prefix = "ba:"
