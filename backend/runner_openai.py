"""runner_openai — BA-owned agent loop over an OpenAI Chat Completions endpoint.

Unlike the claude/gemini/codex runners (which spawn an external CLI that owns
the tool/MCP/approval loop), this runner IS the agent host: it makes HTTP
Chat Completions calls itself and executes tools in-process. There is no
external CLI subprocess.

It plugs into the SAME event/recovery/render-tree funnel as gemini: it writes
only `run_dir/session_events.jsonl` (Claude-shaped lines), `state.json`, and
`complete.json`. The provider (provider_openai.py) tails session_events.jsonl
with GeminiJsonlTailer and feeds apply_event.

v1 scope (deliberate):
  - native mode only (no team/manager orchestration).
  - in-process coding tools: Bash, Read, Write, Edit, Grep, Glob.
  - permission: honor the run's permission mode; gate risky tools behind the
    backend tool-approval round-trip (POST /api/internal/tool-approvals/request)
    unless the mode is bypass.
  - text-only (no image/file attachments yet).
  - per-agent-session OpenAI message history persisted under ba_home() so
    multi-turn resume works (BA owns the conversation store for this kind).
MCP tools + orchestration tools (ask/mssg/delegate) + manager mode are later
phases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from tool_approval_client import describe_tool_call, request_tool_approval

logger = logging.getLogger("runner_openai")

_BASH_TIMEOUT_S = 120
_MAX_OUTPUT_CHARS = 40_000
# Safety bound on the agent tool loop. runner_openai IS the agent host (no
# external CLI like claude/codex/gemini to impose its own limits), so it needs
# an in-process runaway guard. High enough that agentic models (e.g. Sakana
# Fugu, a tool-heavy multi-agent system that routinely needs >40 tool rounds)
# finish naturally; overridable per-run via inputs["max_tool_loops"]. When the
# cap IS hit, the turn is reported as not-completed (see _run's for/else),
# never silently as a successful completion.
_MAX_TOOL_LOOPS = 200
_SESSIONS_SUBDIR = "openai_sessions"
_TOOL_ENV_DENY_EXACT = {
    "better_claude_internal_token",
    "better_agent_internal_token",
}
_TOOL_ENV_DENY_PREFIXES = ("anthropic", "openai")


# --------------------------------------------------------------------------
# small utils
# --------------------------------------------------------------------------

def _now_iso() -> str:
    # localtime with offset, stable enough for event timestamps.
    import datetime
    return datetime.datetime.now().astimezone().isoformat()


def _new_uuid() -> str:
    return uuid.uuid4().hex


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _fail(run_dir: Path, error: str) -> None:
    """Write a failure complete.json so the provider's _watch_complete finalizes."""
    complete = {
        "success": False,
        "session_id": None,
        "error": error,
        "token_usage": None,
        "finished_at": _now_iso(),
    }
    try:
        (run_dir / "complete.json").write_text(
            json.dumps(complete, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("failed to write failure complete.json")


# --------------------------------------------------------------------------
# session message history (BA-owned conversation store)
# --------------------------------------------------------------------------

def _sessions_root() -> Path:
    from paths import ba_home
    root = ba_home() / _SESSIONS_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_path(agent_session_id: str) -> Path:
    # agent_session_id is untrusted-shaped; confine to a single filename.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", agent_session_id)
    return _sessions_root() / f"{safe}.json"


def _load_history(agent_session_id: Optional[str]) -> tuple[str, list[dict]]:
    """Return (session_id, messages). Fresh if no prior history."""
    if agent_session_id:
        p = _session_path(agent_session_id)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return data["session_id"], list(data["messages"])
            except Exception:
                logger.exception("corrupt openai history %s; starting fresh", p)
    sid = agent_session_id or _new_uuid()
    return sid, []


def _save_history(agent_session_id: str, messages: list[dict]) -> None:
    _atomic_write_json(
        _session_path(agent_session_id),
        {"session_id": agent_session_id, "messages": messages},
    )


# --------------------------------------------------------------------------
# event emission (Claude-shaped lines -> session_events.jsonl)
# --------------------------------------------------------------------------

class EventEmitter:
    """Writes normalized Claude-jsonl lines to session_events.jsonl, one uuid
    per logical block, rewriting on each delta. parentUuid advances only when a
    new logical block starts."""

    def __init__(self, events_path: Path) -> None:
        self._fp = events_path.open("a", encoding="utf-8")
        self._parent: Optional[str] = None
        # active accumulation state, keyed by block kind
        self._text_uuid: Optional[str] = None
        self._text_buf: list[str] = []
        self._thinking_uuid: Optional[str] = None
        self._thinking_buf: list[str] = []
        self._tool_calls: dict[int, dict] = {}  # index -> {uuid, id, name, args}
        self._model: str = ""

    def set_model(self, model: str) -> None:
        self._model = model or ""

    def _write(self, event: dict, *, uuid_str: str) -> None:
        event["uuid"] = uuid_str
        event["parentUuid"] = self._parent
        event["timestamp"] = _now_iso()
        self._fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fp.flush()

    def _assistant(self, content: list[dict], uuid_str: str, **extra: Any) -> None:
        msg = {"role": "assistant", "content": content, "model": self._model}
        if extra:
            msg.update(extra)
        self._write({"type": "assistant", "message": msg}, uuid_str=uuid_str)

    def _user(self, content: list[dict], uuid_str: str) -> None:
        msg = {"role": "user", "content": content}
        self._write({"type": "user", "message": msg}, uuid_str=uuid_str)

    # --- streaming deltas ---
    def feed_text_delta(self, chunk: str) -> None:
        if self._text_uuid is None:
            self._text_uuid = _new_uuid()
            self._text_buf = []
        self._text_buf.append(chunk)
        self._assistant(
            [{"type": "text", "text": "".join(self._text_buf)}],
            uuid_str=self._text_uuid,
        )

    def feed_thinking_delta(self, chunk: str) -> None:
        if self._thinking_uuid is None:
            self._thinking_uuid = _new_uuid()
            self._thinking_buf = []
        self._thinking_buf.append(chunk)
        self._assistant(
            [{"type": "thinking", "thinking": "".join(self._thinking_buf)}],
            uuid_str=self._thinking_uuid,
        )

    def feed_tool_call_delta(self, idx: int, tc_id: Optional[str],
                             name: Optional[str], args_delta: Optional[str]) -> None:
        tc = self._tool_calls.get(idx)
        if tc is None:
            tc = {"uuid": _new_uuid(), "id": tc_id or "", "name": name or "",
                  "args": ""}
            self._tool_calls[idx] = tc
        if tc_id:
            tc["id"] = tc_id
        if name:
            tc["name"] = tc["name"] or name
        if args_delta:
            tc["args"] += args_delta
        # emit current accumulated tool_use (id/name may still be partial until
        # the chunk that carries them, but the final delta is authoritative).
        try:
            parsed = json.loads(tc["args"]) if tc["args"] else {}
        except json.JSONDecodeError:
            parsed = {}
        self._assistant(
            [{"type": "tool_use", "id": tc["id"], "name": tc["name"],
              "input": parsed}],
            uuid_str=tc["uuid"],
        )

    def close_text(self) -> Optional[str]:
        """Finalize the text block, advance parent, return the text."""
        if self._text_uuid is not None:
            self._parent = self._text_uuid
            text = "".join(self._text_buf)
            self._text_uuid = None
            self._text_buf = []
            return text
        return None

    def close_thinking(self) -> None:
        if self._thinking_uuid is not None:
            self._parent = self._thinking_uuid
            self._thinking_uuid = None
            self._thinking_buf = []

    def finalize_tool_calls(self) -> list[dict]:
        """Close all open tool_use blocks (advance parent to the last), return
        the finalized tool calls [{id,name,arguments}]."""
        calls = []
        last_uuid: Optional[str] = None
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            last_uuid = tc["uuid"]
            calls.append({"id": tc["id"], "name": tc["name"],
                          "arguments": tc["args"]})
        if calls:
            self._parent = last_uuid
        self._tool_calls = {}
        return calls

    # --- non-streamed events ---
    def emit_tool_result(self, tool_use_id: str, content: str) -> str:
        uid = _new_uuid()
        self._user(
            [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
            uuid_str=uid,
        )
        self._parent = uid
        return uid

    def emit_error(self, error: str) -> None:
        uid = _new_uuid()
        self._assistant(
            [{"type": "text", "text": f"Error: {error}"}],
            uuid_str=uid,
            isApiErrorMessage=True,
        )
        self._parent = uid

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def _confined_path(cwd: Path, raw: str) -> Path:
    """Resolve raw against cwd and refuse escapes (.., absolute, symlink)."""
    cwd = cwd.resolve()
    candidate = (cwd / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    if candidate != cwd and cwd not in candidate.parents:
        raise PermissionError(f"path escapes cwd: {raw}")
    return candidate


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n...[truncated, {len(text)} total chars]"


def _tool_subprocess_env() -> dict[str, str]:
    """Environment visible to model-run shell commands.

    Provider credentials and BA loopback tokens authenticate the runner itself;
    tools must not inherit them. Keep this denylist intentionally broad for
    OpenAI/Anthropic-style env vars and exact for internal-token names so a
    Bash tool cannot read or exfiltrate provider/backend secrets.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        lowered = key.lower()
        if lowered in _TOOL_ENV_DENY_EXACT:
            continue
        if lowered.startswith(_TOOL_ENV_DENY_PREFIXES):
            continue
        env[key] = value
    return env


def _tool_bash(args: dict, cwd: Path) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "Error: empty command"
    timeout = min(int(args.get("timeout") or _BASH_TIMEOUT_S), _BASH_TIMEOUT_S)
    env = _tool_subprocess_env()
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    out = proc.stdout
    if proc.stderr:
        out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
    if proc.returncode != 0:
        out += f"\n[exit {proc.returncode}]"
    return _truncate(out) or "(no output)"


def _tool_read(args: dict, cwd: Path) -> str:
    raw = args.get("file_path") or args.get("path") or ""
    try:
        p = _confined_path(cwd, raw)
    except PermissionError as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: not found: {raw}"
    if p.is_dir():
        return f"Error: is a directory: {raw}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: {e}"
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or 2000)
    lines = text.splitlines()
    sel = lines[max(0, offset - 1): max(0, offset - 1) + limit]
    return _truncate("\n".join(sel))


def _tool_write(args: dict, cwd: Path) -> str:
    raw = args.get("file_path") or args.get("path") or ""
    data = args.get("content")
    if data is None:
        return "Error: missing content"
    try:
        p = _confined_path(cwd, raw)
    except PermissionError as e:
        return f"Error: {e}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")
    return f"wrote {len(data)} bytes to {raw}"


def _tool_edit(args: dict, cwd: Path) -> str:
    raw = args.get("file_path") or args.get("path") or ""
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or new is None:
        return "Error: old_string and new_string required"
    try:
        p = _confined_path(cwd, raw)
    except PermissionError as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: not found: {raw}"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "Error: old_string not found"
    if count > 1 and not args.get("replace_all"):
        return f"Error: old_string matches {count} times; set replace_all to replace all"
    new_text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"edited {raw} ({count} match{'es' if count != 1 else ''})"


def _tool_grep(args: dict, cwd: Path) -> str:
    pattern = args.get("pattern") or ""
    if not pattern:
        return "Error: missing pattern"
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    root_raw = args.get("path") or "."
    try:
        root = _confined_path(cwd, root_raw)
    except PermissionError as e:
        return f"Error: {e}"
    max_files = 200
    hits = []
    base = root if root.is_dir() else root.parent
    for path in base.rglob("*"):
        if len(hits) >= max_files:
            break
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if regex.search(line):
                    rel = path.relative_to(base)
                    hits.append(f"{rel}:{i}:{line[:300]}")
                    if len(hits) >= max_files:
                        break
        except Exception:
            continue
    return _truncate("\n".join(hits)) if hits else "(no matches)"


def _tool_glob(args: dict, cwd: Path) -> str:
    pattern = args.get("pattern") or ""
    if not pattern:
        return "Error: missing pattern"
    root_raw = args.get("path") or "."
    try:
        root = _confined_path(cwd, root_raw)
    except PermissionError as e:
        return f"Error: {e}"
    base = root if root.is_dir() else root.parent
    # simple glob: ** is recursive
    matches = sorted(str(p.relative_to(base)) for p in base.glob(pattern))
    return _truncate("\n".join(matches[:500])) if matches else "(no matches)"


TOOL_HANDLERS = {
    "Bash": _tool_bash,
    "Read": _tool_read,
    "Write": _tool_write,
    "Edit": _tool_edit,
    "Grep": _tool_grep,
    "Glob": _tool_glob,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "Bash", "description": "Run a shell command in the project cwd.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "integer", "description": "Max seconds (<=120)."}},
            "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a text file relative to cwd.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "Write", "description": "Write text to a file (overwrites).",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "Edit", "description": "Replace old_string with new_string in a file.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
            "required": ["file_path", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "Grep", "description": "Regex search files under a path.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "Glob", "description": "Glob match files under a path.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
]

# Better Agent runtime-capability management. Available only when the backend
# loopback channel exists (non-bare, internal sessions); these tools let the
# model scope its own session. Core owns the active-capability write — the tools
# just POST the trigger. Dispatched in `_dispatch_tool`, no permission gate.
_CAPABILITY_TOOL_NAMES = frozenset({
    "list_capabilities", "load_capability", "release_capability",
})
_CAPABILITY_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_capabilities",
        "description": "List the scoped capabilities loadable in this session and which are active.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "load_capability",
        "description": ("Load a scoped capability into this session. Its MCP + skill become "
                        "available on the next turn. Pass the full capability id "
                        "(e.g. 'ofek.testape:testape')."),
        "parameters": {"type": "object", "properties": {
            "capability_id": {"type": "string"}}, "required": ["capability_id"]}}},
    {"type": "function", "function": {
        "name": "release_capability",
        "description": ("Release a previously loaded capability from this session. Pass the "
                        "full capability id (e.g. 'ofek.testape:testape')."),
        "parameters": {"type": "object", "properties": {
            "capability_id": {"type": "string"}}, "required": ["capability_id"]}}},
]


def _tool_schemas_for_run(*, capabilities_enabled: bool) -> list[dict]:
    """Coding tools always; capability-management tools only when the backend
    loopback channel exists for a non-bare session (mirrors the other runners,
    where the capabilities stdio MCP is injected under the same condition)."""
    schemas = list(TOOL_SCHEMAS)
    if capabilities_enabled:
        schemas += _CAPABILITY_TOOL_SCHEMAS
    return schemas


def _is_bypass(permission: Optional[dict]) -> bool:
    if not permission:
        return False
    # permission is CLI-vocab {axis: mode}; treat bypass/yolo as auto-allow.
    vals = " ".join(str(v).lower() for v in permission.values())
    return "bypass" in vals or "yolo" in vals


# --------------------------------------------------------------------------
# Chat Completions streaming
# --------------------------------------------------------------------------

async def _stream_chat(
    base_url: str, api_key: str, model: str, messages: list[dict],
    tools: list[dict],
) -> AsyncIterator[dict]:
    """Yield parsed SSE chunk dicts from a streaming Chat Completions call."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

async def _run(run_dir: Path, inputs: dict) -> int:
    cwd_str = inputs.get("cwd")
    if not cwd_str:
        _fail(run_dir, "missing cwd")
        return 1
    cwd = Path(cwd_str).expanduser().resolve()
    if not cwd.is_dir():
        _fail(run_dir, f"cwd does not exist: {cwd}")
        return 1

    api_key = os.environ.get("OPENAI_API_KEY") or ""
    base_url = os.environ.get("OPENAI_BASE_URL") or ""
    model = inputs.get("model") or ""
    if not api_key or not base_url or not model:
        _fail(run_dir, "OPENAI_API_KEY / OPENAI_BASE_URL / model not configured")
        return 1

    prompt = inputs.get("prompt") or ""
    app_session_id = inputs.get("app_session_id") or _new_uuid()
    permission = inputs.get("permission")
    bypass = _is_bypass(permission)
    backend_url = inputs.get("backend_url") or ""
    internal_token = inputs.get("internal_token") or ""
    # Interactive approval needs the backend HTTP channel. Without it the gate
    # can't surface a prompt — fail closed with a clear message rather than a
    # user-blaming "denied" (mirrors runner.py's interactive_permissions guard).
    interactive = bool(backend_url) and bool(internal_token) and bool(app_session_id)
    # Capability-management tools ride the same backend channel and are stripped
    # from bare (TestApe-isolated) sessions, matching runner.py / the stdio
    # capabilities MCP injected for the CLI providers.
    capabilities_enabled = interactive and not bool(inputs.get("bare_config"))
    tool_schemas = _tool_schemas_for_run(capabilities_enabled=capabilities_enabled)
    resume_sid = inputs.get("session_id")

    session_id, messages = _load_history(resume_sid)
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": _SYSTEM_PROMPT})
    if prompt:
        messages.append({"role": "user", "content": prompt})

    events_path = run_dir / "session_events.jsonl"
    emitter = EventEmitter(events_path)
    emitter.set_model(model)

    # state.json — session_id known immediately; tailer starts on this.
    state = {
        "run_id": run_dir.name, "mode": inputs.get("mode") or "native",
        "runner_pid": os.getpid(), "app_session_id": app_session_id,
        "started_at": _now_iso(), "session_id": session_id,
        "jsonl_path": str(events_path), "complete": False,
    }
    _atomic_write_json(run_dir / "state.json", state)

    usage_acc = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "total_tokens": 0}
    error: Optional[str] = None
    try:
        max_loops = int(inputs.get("max_tool_loops") or _MAX_TOOL_LOOPS)
    except (TypeError, ValueError):
        max_loops = _MAX_TOOL_LOOPS

    try:
        for _ in range(max_loops):
            if (run_dir / "cancel").exists():
                error = "cancelled"
                break
            finish_reason, tool_calls, asst_text, chunk_usage = await _one_round(
                base_url, api_key, model, messages, emitter, run_dir, tool_schemas,
            )
            if (run_dir / "cancel").exists():
                # `_one_round` breaks promptly on the cancel sentinel without
                # raising. Do not save a partial assistant chunk as a successful
                # turn; surface the cancellation and exit non-zero.
                error = "cancelled"
                break
            if chunk_usage:
                usage_acc["input_tokens"] += chunk_usage.get("prompt_tokens", 0)
                usage_acc["output_tokens"] += chunk_usage.get("completion_tokens", 0)
                usage_acc["total_tokens"] += chunk_usage.get("total_tokens", 0)
                pd = chunk_usage.get("prompt_tokens_details") or {}
                usage_acc["cache_read_input_tokens"] += pd.get("cached_tokens", 0) or 0

            # append the assistant turn to the OpenAI messages array. Persist the
            # reply text so resume on turn 2+ carries prior assistant answers.
            # content=None is valid ONLY alongside tool_calls; a round that
            # produced neither text nor tool_calls is dropped rather than
            # persisted as an invalid null-content message that 400s on resume.
            if tool_calls or asst_text is not None:
                asst_msg: dict = {"role": "assistant", "content": asst_text}
                if tool_calls:
                    asst_msg["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in tool_calls
                    ]
                messages.append(asst_msg)

            if not tool_calls or finish_reason == "stop":
                break

            # execute tools
            for call in tool_calls:
                if (run_dir / "cancel").exists():
                    error = "cancelled"
                    break
                result = await _dispatch_tool(
                    call, cwd, app_session_id, run_dir, bypass,
                    interactive, backend_url, internal_token, emitter,
                )
                messages.append({
                    "role": "tool", "tool_call_id": call["id"], "content": result,
                })
            if error:
                break
        else:
            # Loop exhausted the cap without the model emitting a terminal
            # response (finish_reason "stop" / no tool_calls) and without a
            # cancel — i.e. it was still mid-task when the budget ran out.
            # Surface this honestly as a not-completed turn instead of letting
            # error stay None and reporting a truncated turn as success.
            error = (
                f"max tool loops ({max_loops}) reached without the model "
                "ending the turn; raise max_tool_loops or let the model finish"
            )

        _save_history(session_id, messages)
    except Exception as e:
        logger.exception("openai runner loop failed")
        error = f"{type(e).__name__}: {e}"

    if error:
        emitter.emit_error(error)
    emitter.close()

    complete = {
        "success": error is None,
        "session_id": session_id,
        "error": error,
        "token_usage": {**usage_acc, "duration_ms": None},
        "finished_at": _now_iso(),
    }
    _atomic_write_json(run_dir / "complete.json", complete)
    state["complete"] = True
    _atomic_write_json(run_dir / "state.json", state)
    return 0 if error is None else 1


async def _one_round(
    base_url: str, api_key: str, model: str, messages: list[dict],
    emitter: EventEmitter, run_dir: Path, tool_schemas: list[dict] = TOOL_SCHEMAS,
) -> tuple[Optional[str], list[dict], Optional[str], Optional[dict]]:
    """Stream one assistant response. Finalize text/thinking/tool_calls.
    Returns (finish_reason, finalized_tool_calls, assistant_text, usage)."""
    finish_reason: Optional[str] = None
    usage: Optional[dict] = None
    async for chunk in _stream_chat(base_url, api_key, model, messages, tool_schemas):
        if (run_dir / "cancel").exists():
            break
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        fr = choice.get("finish_reason")
        if fr:
            finish_reason = fr
        delta = choice.get("delta") or {}
        if delta.get("content"):
            emitter.feed_text_delta(delta["content"])
        rc = delta.get("reasoning_content")
        if rc:
            emitter.feed_thinking_delta(rc)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            emitter.feed_tool_call_delta(
                idx, tc.get("id"), fn.get("name"), fn.get("arguments"),
            )

    emitter.close_thinking()
    text = emitter.close_text()
    tool_calls = emitter.finalize_tool_calls()
    return finish_reason, tool_calls, text, usage


async def _dispatch_tool(
    call: dict, cwd: Path, app_session_id: str, run_dir: Path,
    bypass: bool, interactive: bool, backend_url: str, internal_token: str,
    emitter: EventEmitter,
) -> str:
    name = call["name"]
    try:
        args = json.loads(call.get("arguments") or "{}")
    except json.JSONDecodeError as e:
        emitter.emit_tool_result(call["id"], f"Error: bad arguments json: {e}")
        return f"Error: bad arguments json: {e}"

    # Capability management tools — no filesystem side effects, no permission
    # gate. They POST to the core capabilities endpoint over the loopback.
    if name in _CAPABILITY_TOOL_NAMES:
        return await _dispatch_capability_tool(
            name=name, args=args, backend_url=backend_url,
            internal_token=internal_token, app_session_id=app_session_id,
            interactive=interactive, emitter=emitter, tool_call_id=call["id"],
        )

    # permission gate: non-bypass runs ask the backend before risky tools
    if not bypass and name in {"Bash", "Write", "Edit"}:
        if not interactive:
            # No approval channel — fail closed honestly, without blaming the
            # user. Bypass mode is the explicit opt-out for ungated execution.
            msg = "Error: approval required but backend approval channel unavailable"
            emitter.emit_tool_result(call["id"], msg)
            return msg
        verdict = await _request_approval(
            app_session_id=app_session_id, run_id=run_dir.name,
            tool_name=name, args=args,
            backend_url=backend_url, internal_token=internal_token,
            cancel_path=run_dir / "cancel",
        )
        if verdict == "cancelled":
            msg = "Error: tool use cancelled by user"
        elif verdict != "approved":
            msg = "Error: tool use denied by user"
        else:
            msg = None
        if msg is not None:
            emitter.emit_tool_result(call["id"], msg)
            return msg

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        emitter.emit_tool_result(call["id"], f"Error: unknown tool: {name}")
        return f"Error: unknown tool: {name}"
    try:
        result = await asyncio.to_thread(handler, args, cwd)
    except PermissionError as e:
        result = f"Error: {e}"
    except Exception as e:
        logger.exception("tool %s failed", name)
        result = f"Error: {type(e).__name__}: {e}"
    emitter.emit_tool_result(call["id"], result)
    return result


async def _wait_cancel(cancel_path: Path) -> bool:
    while True:
        if cancel_path.exists():
            return True
        await asyncio.sleep(0.5)


def _capabilities_endpoint_post(
    *, backend_url: str, internal_token: str, app_session_id: str, payload: dict,
) -> str:
    """POST a capability action (list/load/release) to the core endpoint and
    return a JSON-string result. Core owns the active-capability write; this is
    the authorized trigger. Errors are returned as text, never raised, so a
    failed call doesn't abort the turn."""
    url = backend_url.rstrip("/") + f"/api/internal/sessions/{app_session_id}/capabilities"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": internal_token,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
        if resp.status_code >= 400:
            return f"Error: HTTP {resp.status_code}: {resp.text[:500]}"
        return resp.text or "{}"
    except Exception as e:  # noqa: BLE001 — surface to the model
        return f"Error: {type(e).__name__}: {e}"


async def _dispatch_capability_tool(
    *, name: str, args: dict, backend_url: str, internal_token: str,
    app_session_id: str, interactive: bool, emitter: EventEmitter, tool_call_id: str,
) -> str:
    if not interactive:
        msg = "Error: capabilities require the backend channel (unavailable for this run)"
        emitter.emit_tool_result(tool_call_id, msg)
        return msg
    if name == "list_capabilities":
        payload = {"action": "list"}
    else:
        capability_id = str(args.get("capability_id") or "").strip()
        if not capability_id:
            msg = "Error: capability_id is required"
            emitter.emit_tool_result(tool_call_id, msg)
            return msg
        action = "load" if name == "load_capability" else "release"
        payload = {"action": action, "capability_id": capability_id}
    result = await asyncio.to_thread(
        _capabilities_endpoint_post,
        backend_url=backend_url,
        internal_token=internal_token,
        app_session_id=app_session_id,
        payload=payload,
    )
    emitter.emit_tool_result(tool_call_id, result)
    return result


async def _request_approval(
    *,
    app_session_id: str, run_id: str, tool_name: str, args: dict,
    backend_url: str, internal_token: str, cancel_path: Path,
) -> str:
    """Ask the backend for a human decision. Returns 'approved' | 'denied' |
    'cancelled'. The backend (not this client) is the fail-closed authority:
    any transport error or timeout resolves to 'denied'. A turn-cancel
    sentinel aborts the in-flight request as 'cancelled' instead of waiting
    for the backend timeout."""
    approval_task = asyncio.ensure_future(asyncio.to_thread(
        request_tool_approval,
        backend_url=backend_url,
        internal_token=internal_token,
        app_session_id=app_session_id,
        run_id=run_id,
        provider_kind="openai",
        tool_name=tool_name,
        # Same capped {"tool", "input"} shape as the Claude/Codex runners so
        # the one frontend card renders every argument (was a divergent,
        # un-truncated {"tool", "args"} the UI couldn't read).
        summary=describe_tool_call(tool_name, args),
    ))
    cancel_task = asyncio.ensure_future(_wait_cancel(cancel_path))
    done, pending = await asyncio.wait(
        {approval_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    if cancel_task in done:
        return "cancelled"
    try:
        approved = approval_task.result() if approval_task in done else False
    except Exception:
        # The client swallows errors itself, but defend the contract: any raise
        # out of the thread is a denial, never a turn-aborting exception.
        approved = False
    return "approved" if approved else "denied"


_SYSTEM_PROMPT = (
    "You are a software engineering agent running inside Better Agent's own "
    "agent loop over an OpenAI Chat Completions endpoint. You have tools: "
    "Bash, Read, Write, Edit, Grep, Glob. Work in the project cwd. Be concise. "
    "Use tools to inspect and edit files; do not guess at file contents."
)


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_openai %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as e:
        _fail(run_dir, f"failed to read input.json: {e}")
        return 1
    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as e:
        logger.exception("runner top-level failure")
        _fail(run_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    sys.exit(main(parser.parse_args().run_dir))
