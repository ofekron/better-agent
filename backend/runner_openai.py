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
    in-process tool_approval registry unless the mode is bypass.
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

logger = logging.getLogger("runner_openai")

_BASH_TIMEOUT_S = 120
_MAX_OUTPUT_CHARS = 40_000
_MAX_TOOL_LOOPS = 40
_SESSIONS_SUBDIR = "openai_sessions"


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


def _tool_bash(args: dict, cwd: Path) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "Error: empty command"
    timeout = min(int(args.get("timeout") or _BASH_TIMEOUT_S), _BASH_TIMEOUT_S)
    env = {k: v for k, v in os.environ.items()
           if not k.lower().startswith(("anthropic", "openai"))}
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
        for _ in range(_MAX_TOOL_LOOPS):
            if (run_dir / "cancel").exists():
                error = "cancelled"
                break
            finish_reason, tool_calls, chunk_usage = await _one_round(
                base_url, api_key, model, messages, emitter, run_dir,
            )
            if chunk_usage:
                usage_acc["input_tokens"] += chunk_usage.get("prompt_tokens", 0)
                usage_acc["output_tokens"] += chunk_usage.get("completion_tokens", 0)
                usage_acc["total_tokens"] += chunk_usage.get("total_tokens", 0)
                pd = chunk_usage.get("prompt_tokens_details") or {}
                usage_acc["cache_read_input_tokens"] += pd.get("cached_tokens", 0) or 0

            # append the assistant turn to the OpenAI messages array
            asst_msg: dict = {"role": "assistant", "content": None}
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
                result = await _dispatch_tool(call, cwd, app_session_id,
                                              run_dir.name, bypass, permission, emitter)
                messages.append({
                    "role": "tool", "tool_call_id": call["id"], "content": result,
                })
            if error:
                break

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
    emitter: EventEmitter, run_dir: Path,
) -> tuple[Optional[str], list[dict], Optional[dict]]:
    """Stream one assistant response. Finalize text/thinking/tool_calls.
    Returns (finish_reason, finalized_tool_calls, usage_from_final_chunk)."""
    finish_reason: Optional[str] = None
    usage: Optional[dict] = None
    async for chunk in _stream_chat(base_url, api_key, model, messages, TOOL_SCHEMAS):
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
    emitter.close_text()
    tool_calls = emitter.finalize_tool_calls()
    return finish_reason, tool_calls, usage


async def _dispatch_tool(
    call: dict, cwd: Path, app_session_id: str, run_id: str,
    bypass: bool, permission: Optional[dict], emitter: EventEmitter,
) -> str:
    name = call["name"]
    try:
        args = json.loads(call.get("arguments") or "{}")
    except json.JSONDecodeError as e:
        emitter.emit_tool_result(call["id"], f"Error: bad arguments json: {e}")
        return f"Error: bad arguments json: {e}"

    # permission gate: non-bypass runs ask before risky tools
    if not bypass and name in {"Bash", "Write", "Edit"}:
        approved = await _request_approval(
            app_session_id, run_id, name, args, permission,
        )
        if not approved:
            emitter.emit_tool_result(call["id"], "Error: tool use denied by user")
            return "Error: tool use denied by user"

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


async def _request_approval(
    app_session_id: str, run_id: str, tool_name: str, args: dict,
    permission: Optional[dict],
) -> bool:
    try:
        from tool_approval import registry
    except Exception:
        return True  # backend module unavailable in this runner context -> allow
    rec = registry.create(
        app_session_id=app_session_id, run_id=run_id, provider_kind="openai",
        tool_name=tool_name,
        summary={"tool": tool_name, "args": args},
    )
    return await registry.await_decision(rec)


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
