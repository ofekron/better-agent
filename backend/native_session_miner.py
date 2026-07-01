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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from paths import bc_home, claude_projects_root_for_session, encode_cwd
from session_miner import SessionMinerBase, SessionVisit, sessions_dir


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

    def parse(self) -> SessionVisit | None:
        try:
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
    index: dict[str, Path] = {}
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
