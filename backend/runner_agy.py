from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from builtin_mcp_config import native_mcp_runtime_env, with_builtin_mcp_servers
from cli_paths import resolve_cli_binary
from prompt_templates import render_prompt
from provider_run_config import symlink_home_overlay, write_skill_tree

logger = logging.getLogger(__name__)
_CONVERSATION_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_AGY_MESSAGE_RE = re.compile(
    r"^\[Message\]\s+timestamp=(?P<timestamp>\S+)\s+sender=(?P<sender>[0-9a-fA-F-]{36})\s+priority=\S+\s+content=(?P<content>.*)$",
    re.S,
)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _assistant_event(text: str, *, model: Optional[str], parent_uuid: str) -> dict[str, Any]:
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": model or "agy",
            },
            "uuid": _new_uuid(),
            "parentUuid": parent_uuid,
            "timestamp": datetime.now().isoformat(),
            "parent_tool_use_id": None,
        },
    }


# Deterministic UUID namespace so streamed and post-exit emissions of the same
# logical event collide in the render tree's uuid dedup instead of duplicating.
_AGY_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.runner_agy.events")
# How often the streaming watcher polls the agy conversation DB for new steps.
# agy steps are append-only by `idx` PRIMARY KEY, so each new step lands at a
# stable position in _agy_worker_events' output — we emit events[emitted:].
_STREAM_INTERVAL = 0.5


def _event_uuid_holder(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the dict whose `uuid` apply_event dedups on, or None.

    Handles worker_event wrappers (data.event.data.uuid) and canonical
    agent_message (data.uuid). worker_start/worker_complete carry no uuid.
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    inner = data.get("event")
    if isinstance(inner, dict):
        inner_data = inner.get("data")
        if isinstance(inner_data, dict):
            return inner_data
    return data if "uuid" in data else None


def _stabilize_event_uuids(events: list[dict[str, Any]], conversation_id: Optional[str]) -> None:
    """Assign uuid5(... conversation_id|index) so re-emission is idempotent."""
    if not conversation_id:
        return
    for i, event in enumerate(events):
        holder = _event_uuid_holder(event)
        if holder is not None:
            holder["uuid"] = str(uuid.uuid5(_AGY_UUID_NAMESPACE, f"{conversation_id}|{i}"))


def _stream_new_events(
    events_path: Path,
    *,
    agy_home: Path,
    conversation_id: Optional[str],
    parent_uuid: str,
    emitted: dict[str, int],
) -> None:
    """Append agy steps not yet written to session_events.jsonl.

    Emits each event exactly once by stable output index; safe to call
    repeatedly during the run and once more as the final post-exit flush.
    """
    if not conversation_id:
        return
    events = _agy_worker_events(
        agy_home=agy_home,
        conversation_id=conversation_id,
        parent_uuid=parent_uuid,
    )
    _stabilize_event_uuids(events, conversation_id)
    new = events[emitted["count"]:]
    if not new:
        return
    with events_path.open("a", encoding="utf-8") as fh:
        for event in new:
            fh.write(json.dumps(event) + "\n")
    emitted["count"] = len(events)


def _load_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _materialize_agy_run_home(run_dir: Path, provider_run_config: dict) -> Optional[dict[str, str]]:
    mcp_servers = provider_run_config.get("mcp_servers") or {}
    skills = provider_run_config.get("skills") or {}
    if not mcp_servers and not skills:
        return None

    real_home = Path.home()
    overlay_home = run_dir / "agy-home"
    # agy has no config-dir env var (unlike the gemini CLI's GEMINI_CLI_HOME),
    # so it hard-wires $HOME/.gemini/antigravity-cli and reads its OAuth
    # credential from $HOME/Library. The scoped HOME must therefore carry the
    # real home top-level — including Library — or agy can't authenticate and
    # every run fails with "authentication timed out". Skip .gemini/.agents;
    # the dedicated mirrors below overlay per-run settings/skills onto those.
    symlink_home_overlay(real_home, overlay_home, skip={".gemini", ".agents"})
    symlink_home_overlay(real_home / ".gemini", overlay_home / ".gemini", skip={"antigravity-cli"})
    real_cli = real_home / ".gemini" / "antigravity-cli"
    overlay_cli = overlay_home / ".gemini" / "antigravity-cli"
    symlink_home_overlay(real_cli, overlay_cli, skip={"settings.json", "builtin"})
    symlink_home_overlay(real_home / ".agents", overlay_home / ".agents", skip={"skills"})

    settings = _load_json_object(real_cli / "settings.json")
    if mcp_servers:
        settings["mcpServers"] = mcp_servers
    if skills:
        settings["skills"] = {"enabled": True}
    if settings:
        overlay_cli.mkdir(parents=True, exist_ok=True)
        (overlay_cli / "settings.json").write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if skills:
        write_skill_tree(overlay_cli / "builtin" / "skills", skills)
        write_skill_tree(overlay_home / ".agents" / "skills", skills)
    return {"HOME": str(overlay_home)}


def _prepend_capability_context(prompt: str, inputs: dict) -> str:
    blocks: list[str] = []
    for item in inputs.get("capability_contexts") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        name = str(item.get("name") or "Capability")
        category = str(item.get("category") or "capability")
        blocks.append(f"## {name} ({category})\n\n{content.strip()}")
    if not blocks:
        return prompt
    prefix = render_prompt(
        "runner/capability_context.md",
        {"blocks": "\n\n".join(blocks)},
    )
    return f"{prefix}\n\n{prompt}" if prompt else prefix


def _materialize_attachments(run_dir: Path, images: list) -> list[Path]:
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        ext = img["media_type"].split("/")[-1].replace("jpeg", "jpg")
        fpath = att_dir / f"attachment_{i}.{ext}"
        fpath.write_bytes(base64.b64decode(img["data"]))
        paths.append(fpath)
    return paths


def _apply_image_attachments(run_dir: Path, prompt: str, images: list) -> tuple[str, Optional[Path]]:
    if not images:
        return prompt, None
    paths = _materialize_attachments(run_dir, images)
    refs = "\n".join(f"@{path}" for path in paths)
    return (f"{prompt}\n\n{refs}" if prompt else refs), paths[0].parent


def _apply_file_attachments(prompt: str, files: list) -> str:
    if not files:
        return prompt
    file_sections: list[str] = []
    for item in files:
        raw = base64.b64decode(item.get("data", ""))
        name = item.get("name", "unknown")
        try:
            text = raw.decode("utf-8")
            file_sections.append(f"<file name=\"{name}\">\n{text}\n</file>")
        except UnicodeDecodeError:
            file_sections.append(
                f"<file name=\"{name}\">[binary file, {item.get('size', len(raw))} bytes]</file>"
            )
    preamble = "\n\n".join(file_sections)
    return f"{preamble}\n\n{prompt}" if prompt else preamble


def _agy_root(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli"


def _conversation_exists(home: Path, conversation_id: Optional[str]) -> bool:
    return bool(conversation_id) and _conversation_db(home, str(conversation_id)).is_file()


def _last_conversation_for_cwd(home: Path, cwd: str) -> Optional[str]:
    path = _agy_root(home) / "cache" / "last_conversations.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get(cwd)
    if isinstance(sid, str) and _conversation_exists(home, sid):
        return sid
    return None


def _resolve_resume_conversation(home: Path, cwd: str, requested: str) -> str:
    if _conversation_exists(home, requested):
        return requested
    return _last_conversation_for_cwd(home, cwd) or ""


def _discover_conversation_id(
    log_path: Path,
    *,
    preferred: Optional[str],
    agy_home: Path,
    cwd: str,
) -> Optional[str]:
    if not log_path.is_file():
        if _conversation_exists(agy_home, preferred):
            return preferred
        return _last_conversation_for_cwd(agy_home, cwd)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        if _conversation_exists(agy_home, preferred):
            return preferred
        return _last_conversation_for_cwd(agy_home, cwd)
    parent_markers = (
        "Print mode: conversation=",
        "Created conversation ",
        "Streaming conversation ",
        "Forwarding user message to conversation ",
        "Sending user message to conversation ",
    )
    for marker in parent_markers:
        for line in text.splitlines():
            if marker not in line:
                continue
            match = _CONVERSATION_RE.search(line)
            if match:
                return match.group(0)
    if _conversation_exists(agy_home, preferred):
        return preferred
    return _last_conversation_for_cwd(agy_home, cwd)


def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
    _write_json(run_dir / "state.json", state)


def _strings_from_blob(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8", "replace")
    else:
        raw = bytes(value)
    out: list[str] = []
    for match in re.findall(rb"[ -~]{3,}", raw):
        text = match.decode("utf-8", "replace").strip()
        if text:
            out.append(text)
    return out


def _json_object_from_strings(strings: list[str]) -> Optional[dict[str, Any]]:
    for text in strings:
        start = text.find("{")
        if start < 0:
            continue
        candidate = text[start:].rstrip(":")
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _leading_tokens(text: str) -> list[str]:
    return text.split("{", 1)[0].rstrip(":").split()


def _valid_tool_name(text: str) -> bool:
    return (
        text not in {"sessionID", "agent_message"}
        and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", text) is not None
    )


def _looks_like_tool_json(text: str) -> bool:
    candidate = text.strip().lstrip("|").rstrip(":")
    if not candidate.startswith("{"):
        return False
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and (
        "toolAction" in parsed or "toolSummary" in parsed
    )


def _meaningful_text(strings: list[str]) -> Optional[str]:
    skip_prefixes = (
        "sessionID",
        "agent_message",
        "toolAction",
        "toolSummary",
    )
    candidates: list[str] = []
    for text in strings:
        if _UUID_RE.fullmatch(text.strip('"$')):
            continue
        if len(text) < 96 and _CONVERSATION_RE.search(text):
            continue
        if any(text.startswith(prefix) for prefix in skip_prefixes):
            continue
        if "bot-" in text:
            continue
        if text.startswith("{") or text.endswith("}"):
            continue
        if _looks_like_tool_json(text):
            continue
        if len(text) < 12:
            continue
        if not re.search(r"\s", text):
            continue
        if not re.search(r"[a-zA-Z]{3}", text):
            continue
        candidates.append(text)
    if not candidates:
        return None
    return max(candidates, key=len)


def _agent_message(
    *,
    role: str,
    content: list[dict[str, Any]],
    parent_uuid: str,
    model: str = "agy",
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": role,
        "content": content,
    }
    if role == "assistant":
        message["model"] = model
    return {
        "type": "agent_message",
        "data": {
            "type": role,
            "message": message,
            "uuid": _new_uuid(),
            "parentUuid": parent_uuid,
            "timestamp": timestamp or datetime.now().isoformat(),
            "parent_tool_use_id": None,
        },
    }


def _tool_use_event(
    *,
    tool_id: str,
    name: str,
    input_data: dict[str, Any],
    parent_uuid: str,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _agent_message(
        role="assistant",
        content=[{
            "type": "tool_use",
            "id": tool_id or _new_uuid(),
            "name": name,
            "input": input_data,
        }],
        parent_uuid=parent_uuid,
        timestamp=timestamp,
    )


def _tool_result_event(
    *,
    tool_id: str,
    content: str,
    parent_uuid: str,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    return _agent_message(
        role="user",
        content=[{
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content,
        }],
        parent_uuid=parent_uuid,
        timestamp=timestamp,
    )


def _conversation_db(root: Path, conversation_id: str) -> Path:
    return root / ".gemini" / "antigravity-cli" / "conversations" / f"{conversation_id}.db"


def _read_agy_steps(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "select idx, step_type, status, has_subtrajectory, metadata, step_payload, render_info "
            "from steps order by idx"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for idx, step_type, status, has_subtrajectory, metadata, payload, render_info in rows:
        strings = (
            _strings_from_blob(metadata)
            + _strings_from_blob(payload)
            + _strings_from_blob(render_info)
        )
        out.append({
            "idx": idx,
            "step_type": step_type,
            "status": status,
            "has_subtrajectory": bool(has_subtrajectory),
            "strings": strings,
            "json": _json_object_from_strings(strings),
        })
    return out


def _extract_parent_subagent_events(
    *,
    db_path: Path,
    parent_uuid: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    subagents: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []

    for step in _read_agy_steps(db_path):
        payload = step.get("json") or {}
        tool = next(
            (name for name in ("invoke_subagent", "send_message") if any(name in s for s in step["strings"])),
            "",
        )
        if tool == "invoke_subagent" and step.get("step_type") == 127:
            tool_id = f"agy-step-{step['idx']}"
            if step["strings"]:
                first_token = step["strings"][0].split(" ", 1)[0].strip()
                if first_token:
                    tool_id = first_token
            for i, item in enumerate(payload.get("Subagents") or []):
                if not isinstance(item, dict):
                    continue
                pending.append({
                    "tool_id": f"{tool_id}-{i}",
                    "prompt": str(item.get("Prompt") or ""),
                    "role": str(item.get("Role") or item.get("TypeName") or "AGY subagent"),
                    "type": str(item.get("TypeName") or "subagent"),
                    "insert_at": len(events),
                })
            continue

        for text in step["strings"]:
            match = _AGY_MESSAGE_RE.match(text)
            if not match:
                continue
            sender = match.group("sender")
            content = match.group("content").strip()
            timestamp = match.group("timestamp")
            info = subagents.get(sender)
            if info is None:
                info = pending.pop(0) if pending else {
                    "tool_id": f"agy-{sender}",
                    "prompt": "",
                    "role": f"AGY subagent {sender[:8]}",
                    "type": "subagent",
                    "insert_at": len(events),
                }
                info["sender"] = sender
                info["delegation_id"] = f"agy_subagent_{sender}"
                subagents[sender] = info
                events.append({"type": "worker_start", "data": {
                    "delegation_id": info["delegation_id"],
                    "worker_session_id": sender,
                    "worker_description": info["role"],
                    "panel_kind": "worker",
                    "started_at": timestamp,
                    "insert_at": info.get("insert_at", len(events)),
                    "orchestration_mode": "native",
                    "is_new": False,
                    "instructions_preview": info.get("prompt") or "",
                    "run_mode": "agy_subagent",
                    "fork_agent_sid": sender,
                }})
                events.append({"type": "worker_event", "data": {
                    "delegation_id": info["delegation_id"],
                    "event": _tool_use_event(
                        tool_id=info["tool_id"],
                        name="Agent",
                        input_data={
                            "subagent_type": info.get("type") or "subagent",
                            "description": info.get("role") or "AGY subagent",
                            "prompt": info.get("prompt") or "",
                        },
                        parent_uuid=parent_uuid,
                        timestamp=timestamp,
                    ),
                }})
            events.append({"type": "worker_event", "data": {
                "delegation_id": info["delegation_id"],
                "event": _tool_result_event(
                    tool_id=info["tool_id"],
                    content=content,
                    parent_uuid=parent_uuid,
                    timestamp=timestamp,
                ),
            }})
    return events, subagents


def _extract_subagent_conversation_events(
    *,
    db_path: Path,
    delegation_id: str,
    parent_uuid: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    last_tool_id = ""
    steps = _read_agy_steps(db_path)
    prompt_texts = {
        text
        for step in steps
        if step.get("step_type") == 14
        for text in step["strings"]
        if len(text) >= 24 and re.search(r"\s", text)
    }
    for step in steps:
        if step.get("step_type") == 14:
            continue
        strings = step["strings"]
        payload = step.get("json") or {}
        if payload:
            tool_id = ""
            tool_name = ""
            if strings:
                tokens = _leading_tokens(strings[0])
                if len(tokens) > 1 and _valid_tool_name(tokens[1]):
                    tool_id = tokens[0]
                    tool_name = tokens[1]
            if not tool_name and len(strings) > 1 and _valid_tool_name(strings[1]):
                tool_id = strings[0]
                tool_name = strings[1]
            if tool_id and tool_name:
                last_tool_id = tool_id
                events.append({"type": "worker_event", "data": {
                    "delegation_id": delegation_id,
                    "event": _tool_use_event(
                        tool_id=tool_id,
                        name=tool_name,
                        input_data=payload,
                        parent_uuid=parent_uuid,
                    ),
                }})
                continue
        text = _meaningful_text(strings)
        if not text or text in prompt_texts:
            continue
        if step.get("step_type") in {7, 8, 9, 23, 101, 127, 132} and last_tool_id:
            inner = _tool_result_event(
                tool_id=last_tool_id,
                content=text,
                parent_uuid=parent_uuid,
            )
        else:
            inner = _agent_message(
                role="assistant",
                content=[{"type": "text", "text": text}],
                parent_uuid=parent_uuid,
            )
        events.append({"type": "worker_event", "data": {
            "delegation_id": delegation_id,
            "event": inner,
        }})
    return events


def _agy_worker_events(
    *,
    agy_home: Path,
    conversation_id: Optional[str],
    parent_uuid: str,
) -> list[dict[str, Any]]:
    if not conversation_id:
        return []
    parent_db = _conversation_db(agy_home, conversation_id)
    parent_events, subagents = _extract_parent_subagent_events(
        db_path=parent_db,
        parent_uuid=parent_uuid,
    )
    events = list(parent_events)
    for sender, info in subagents.items():
        child_db = _conversation_db(agy_home, sender)
        events.extend(_extract_subagent_conversation_events(
            db_path=child_db,
            delegation_id=info["delegation_id"],
            parent_uuid=parent_uuid,
        ))
        events.append({"type": "worker_complete", "data": {
            "delegation_id": info["delegation_id"],
            "worker_session_id": sender,
            "success": True,
            "error": None,
            "fork_agent_sid": sender,
            "run_mode": "agy_subagent",
        }})
    return events


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_agy fatal: %s", error)
    _write_json(
        run_dir / "complete.json",
        {
            "success": False,
            "session_id": None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        },
    )


def _auth_failure_from_output(stdout: str, stderr: str) -> Optional[str]:
    combined = f"{stdout}\n{stderr}"
    if "Authentication required. Please visit the URL to log in:" not in combined:
        return None
    if "Error: authentication timed out." in combined:
        return "Antigravity authentication timed out. Log in with the agy CLI and retry."
    return "Antigravity authentication is required. Log in with the agy CLI and retry."


async def _run(run_dir: Path, inputs: dict[str, Any]) -> int:
    agy_bin = resolve_cli_binary("agy")
    if not agy_bin:
        _fail(run_dir, "agy CLI not found on PATH")
        return 1

    prompt = str(inputs.get("prompt") or "")
    prompt = _prepend_capability_context(prompt, inputs)
    prompt = _apply_file_attachments(prompt, inputs.get("files") or [])
    prompt, attachment_dir = _apply_image_attachments(run_dir, prompt, inputs.get("images") or [])
    model = str(inputs.get("model") or "").strip()
    cwd = str(inputs.get("cwd") or os.getcwd())
    session_id = str(inputs.get("session_id") or "").strip()
    if not prompt:
        _fail(run_dir, "missing required field: prompt")
        return 1

    stderr_path = run_dir / "agy_stderr.log"
    run_env = os.environ.copy()
    run_env.update(native_mcp_runtime_env(inputs))
    provider_run_config = with_builtin_mcp_servers(inputs, inputs.get("provider_run_config") or {})
    scoped_env = _materialize_agy_run_home(run_dir, provider_run_config)
    if scoped_env:
        run_env.update(scoped_env)
    agy_home = Path(run_env.get("HOME") or str(Path.home()))
    resume_session_id = _resolve_resume_conversation(agy_home, cwd, session_id) if session_id else ""

    state = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": resume_session_id or None,
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "complete": False,
    }
    if session_id or resume_session_id:
        _write_state(run_dir, state)

    argv = [agy_bin]
    if model:
        argv += ["--model", model]
    if resume_session_id:
        argv += ["--conversation", resume_session_id]
    argv += ["--add-dir", cwd, "--print-timeout", "24h"]
    if attachment_dir:
        argv += ["--add-dir", str(attachment_dir)]
    log_path = run_dir / "agy_cli.log"
    argv += ["--log-file", str(log_path), "-p", prompt]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=run_env,
    )
    cancel_path = run_dir / "cancel"
    cancelled = False
    events_path = run_dir / "session_events.jsonl"
    # Mutable holder so the streaming watcher and the final flush share one
    # emit cursor — every event is written to disk exactly once.
    emitted: dict[str, int] = {"count": 0}

    async def _watch_cancel() -> None:
        nonlocal cancelled
        while proc.returncode is None:
            if cancel_path.exists():
                cancelled = True
                proc.terminate()
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
                return
            await asyncio.sleep(0.15)

    async def _watch_conversation() -> None:
        while proc.returncode is None:
            sid = _discover_conversation_id(
                log_path,
                preferred=resume_session_id or None,
                agy_home=agy_home,
                cwd=cwd,
            )
            if sid:
                state["session_id"] = sid
                _write_state(run_dir, state)
                return
            await asyncio.sleep(0.15)

    async def _watch_stream() -> None:
        # Stream agy steps into session_events.jsonl as they land so the
        # provider's polling tailer can feed the render tree during the turn.
        # Without this, nothing is emitted until agy exits, so a long or hung
        # agy turn renders as an empty bubble stuck "streaming" forever.
        while proc.returncode is None:
            sid = state.get("session_id")
            if sid:
                _stream_new_events(
                    events_path,
                    agy_home=agy_home,
                    conversation_id=sid,
                    parent_uuid=sid,
                    emitted=emitted,
                )
            await asyncio.sleep(_STREAM_INTERVAL)

    cancel_task = asyncio.create_task(_watch_cancel())
    conversation_task = asyncio.create_task(_watch_conversation())
    stream_task = asyncio.create_task(_watch_stream())
    try:
        stdout_bytes, stderr_bytes = await proc.communicate()
    finally:
        for task in (cancel_task, conversation_task, stream_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    stdout = stdout_bytes.decode(errors="replace").strip()
    stderr = stderr_bytes.decode(errors="replace").strip()
    if stderr:
        stderr_path.write_text(stderr, encoding="utf-8")

    discovered_sid = _discover_conversation_id(
        log_path,
        preferred=resume_session_id or None,
        agy_home=agy_home,
        cwd=cwd,
    )
    if discovered_sid:
        state["session_id"] = discovered_sid
        _write_state(run_dir, state)

    auth_error = _auth_failure_from_output(stdout, stderr)
    success = proc.returncode == 0 and not cancelled and auth_error is None
    error = None if success else (
        "cancelled"
        if cancelled else
        auth_error or stderr or f"agy CLI exited with code {proc.returncode}"
    )
    parent_uuid = state.get("session_id") or _new_uuid()
    # Final flush: any steps added after the last stream poll, then the
    # terminal assistant message. Re-runs _agy_worker_events in full but only
    # writes events past the shared emit cursor, so nothing duplicates.
    _stream_new_events(
        events_path,
        agy_home=agy_home,
        conversation_id=state.get("session_id"),
        parent_uuid=parent_uuid,
        emitted=emitted,
    )
    final_event = _assistant_event(
        stdout if success else f"Error: {error}",
        model=model,
        parent_uuid=parent_uuid,
    )
    final_seed = f"{state.get('session_id') or ''}|final"
    final_event["data"]["uuid"] = str(uuid.uuid5(_AGY_UUID_NAMESPACE, final_seed))
    with events_path.open("a", encoding="utf-8") as events:
        events.write(json.dumps(final_event) + "\n")

    state["complete"] = True
    state["finished_at"] = datetime.now().isoformat()
    _write_state(run_dir, state)
    _write_json(
        run_dir / "complete.json",
        {
            "success": success,
            "session_id": state["session_id"],
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        },
    )
    return 0 if success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_agy %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as exc:
        _fail(run_dir, f"failed to read input.json: {exc}")
        return 1
    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as exc:
        logger.exception("runner_agy top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
