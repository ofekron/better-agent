"""Codex rollout/stream normalization primitives.

Pure parsing: raw Codex payloads/items -> Claude-shaped events.
BA-import-free and stdlib-only so it can be shared by the live runner
(runner_codex), offline replay/tailing (codex_native), and vendored into
the transcript-search product.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _codex_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _codex_primary_assistant_text(record: dict) -> str:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
        message = payload.get("message")
        return message.strip() if isinstance(message, str) else ""
    if (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
    ):
        return _codex_text_content(payload.get("content"))
    return ""


def _codex_terminal_state(record: dict) -> Optional[bool]:
    payload = record.get("payload")
    if record.get("type") == "event_msg" and isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "task_complete":
            return True
        if payload_type in ("task_failed", "turn_failed"):
            return False
    if record.get("type") == "turn.completed":
        return True
    if record.get("type") == "turn.failed":
        return False
    return None


def _codex_reasoning_text(payload: dict) -> str:
    text = _codex_text_content(payload.get("summary"))
    if text:
        return text
    return _codex_text_content(payload.get("content"))

def _new_uuid() -> str:
    return str(uuid.uuid4())


def _stable_uuid(namespace: str, key: str) -> str:
    """Deterministic UUID from (namespace, key). Same inputs → same
    UUID across re-emissions so `apply_event` REPLACEs the render-tree
    node in place instead of appending a card per update. `namespace`
    (the codex thread_id) keeps the reused `item_0` id from colliding
    across turns/sessions."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{key}"))


def _stable_payload_key(payload: dict) -> str:
    item_id = payload.get("id") or payload.get("call_id")
    if isinstance(item_id, str) and item_id:
        return item_id
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _response_item_uuid(parent_uuid: str, payload: dict, suffix: str = "") -> str:
    payload_type = payload.get("type") or "unknown"
    return _stable_uuid(
        parent_uuid,
        f"response_item:{payload_type}:{_stable_payload_key(payload)}{suffix}",
    )


def _file_size(path: Optional[Path]) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ============================================================================
# Tool name mapping — Codex → Claude
# ============================================================================
_TOOL_NAME_MAP = {
    "command_execution": "Bash",
    "file_change": "Edit",
    "mcp_tool_call": "MCP",
}

_CODEX_AGENT_TOOL_NAMES = {
    "spawn_agent",
    "spawn_agents",
    "spawn_agents_on_csv",
    "multi_agent.spawn_agent",
    "multi_agent_v1.spawn_agent",
}


# ============================================================================
# Event normalization — Codex ThreadEvent → Claude jsonl shape
# ============================================================================

def _normalize_agent_message(
    item: dict, parent_uuid: str, *, event_uuid: Optional[str] = None,
) -> dict:
    text = item.get("text", "")
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "codex",
        },
        "uuid": event_uuid or _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, item)


def _normalize_reasoning(
    item: dict, parent_uuid: str, *, event_uuid: Optional[str] = None,
) -> dict:
    text = item.get("text", "")
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": "codex",
        },
        "uuid": event_uuid or _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, item)


def _normalize_command_started(item: dict, parent_uuid: str) -> dict:
    command = item.get("command", "")
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id", _new_uuid()),
                "name": "Bash",
                "input": {"command": command},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_command_completed(item: dict, parent_uuid: str) -> dict:
    output = item.get("aggregated_output", "")
    exit_code = item.get("exit_code")
    status = item.get("status", "completed")
    content = output
    if status == "failed" and exit_code is not None and exit_code != 0:
        content = f"Error: exit code {exit_code}\n{output}"
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": content or "",
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_file_change(item: dict, parent_uuid: str) -> dict:
    changes = item.get("changes", [])
    status = item.get("status", "completed")
    parts = []
    for change in changes:
        path = change.get("path", "")
        kind = change.get("kind", "update")
        if kind == "delete":
            parts.append(f"Delete: {path}")
        elif kind == "add":
            parts.append(f"Add: {path}")
        else:
            parts.append(f"Update: {path}")
    description = "\n".join(parts)
    tool_name = "Edit" if status != "failed" else "Edit"
    result = description if status == "completed" else f"Failed: {description}"
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id", _new_uuid()),
                "name": tool_name,
                "input": {"description": description},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": result,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_mcp_tool_started(item: dict, parent_uuid: str) -> dict:
    tool_name = item.get("tool", "unknown")
    server = item.get("server", "")
    arguments = item.get("arguments", {})
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id", _new_uuid()),
                "name": f"mcp__{server}__{tool_name}" if server else tool_name,
                "input": arguments if isinstance(arguments, dict) else {"value": arguments},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_mcp_tool_completed(item: dict, parent_uuid: str) -> dict:
    error = item.get("error")
    result_data = item.get("result")
    content = ""
    if error:
        if isinstance(error, dict):
            content = f"Error: {error.get('message', str(error))}"
        else:
            content = f"Error: {error}"
    elif result_data:
        result_content = result_data.get("content", [])
        if isinstance(result_content, list):
            texts = []
            for c in result_content:
                if isinstance(c, dict):
                    texts.append(c.get("text", json.dumps(c)))
                else:
                    texts.append(str(c))
            content = "\n".join(texts)
        else:
            content = str(result_data)
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _collab_agent_description(item: dict) -> str:
    prompt = item.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    receivers = item.get("receiverThreadIds")
    if isinstance(receivers, list) and receivers:
        return f"{item.get('tool') or 'subagent'}: {', '.join(str(r) for r in receivers)}"
    return str(item.get("tool") or "subagent")


def _normalize_collab_agent_started(item: dict, parent_uuid: str) -> dict:
    description = _collab_agent_description(item)
    input_data = {
        "subagent_type": str(item.get("tool") or "default"),
        "description": description,
        "prompt": item.get("prompt") or description,
    }
    if item.get("model"):
        input_data["model"] = item["model"]
    if item.get("reasoningEffort"):
        input_data["reasoning_effort"] = item["reasoningEffort"]
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id") or _new_uuid(),
                "name": "Agent",
                "input": input_data,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_collab_agent_completed(item: dict, parent_uuid: str) -> dict:
    states = item.get("agentsStates")
    lines: list[str] = []
    if isinstance(states, dict):
        for thread_id, state in states.items():
            if not isinstance(state, dict):
                continue
            status = state.get("status")
            message = state.get("message")
            text = " ".join(str(v) for v in (status, message) if v)
            if text:
                lines.append(f"{thread_id}: {text}")
    content = "\n".join(lines) or str(item.get("status") or "completed")
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": item.get("id", ""),
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_web_search(item: dict, parent_uuid: str, tool_id: Optional[str] = None) -> dict:
    query = item.get("query", "")
    action = item.get("action", "")
    tool_use_id = tool_id or item.get("id") or _new_uuid()
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "WebSearch",
                "input": {"query": query, "action": action},
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _web_search_result_text(item: dict) -> str:
    result_data = None
    for key in ("result", "results", "output", "content"):
        if key in item:
            result_data = item.get(key)
            break
    if not result_data:
        return ""

    if isinstance(result_data, str):
        return result_data.strip()

    if isinstance(result_data, list):
        parts = []
        for entry in result_data:
            if isinstance(entry, dict):
                title = entry.get("title") or entry.get("name") or ""
                url = entry.get("url") or entry.get("link") or ""
                snippet = (
                    entry.get("snippet")
                    or entry.get("text")
                    or entry.get("content")
                    or entry.get("summary")
                    or ""
                )
                line = " - ".join(str(v) for v in (title, url, snippet) if v)
                if line:
                    parts.append(line)
            elif entry is not None:
                parts.append(str(entry))
        return "\n".join(parts).strip()

    if isinstance(result_data, dict):
        content = result_data.get("content")
        if isinstance(content, list):
            texts = []
            for entry in content:
                if isinstance(entry, dict):
                    text = entry.get("text") or entry.get("content")
                    if text:
                        texts.append(str(text))
                elif entry is not None:
                    texts.append(str(entry))
            if texts:
                return "\n".join(texts).strip()
        for key in ("text", "snippet", "summary", "content"):
            value = result_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(result_data, ensure_ascii=False)

    return str(result_data).strip()


def _normalize_web_search_result(
    item: dict,
    parent_uuid: str,
    tool_id: Optional[str] = None,
) -> Optional[dict]:
    content = _web_search_result_text(item)
    if not content:
        return None
    tool_use_id = tool_id or item.get("id") or ""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _normalize_web_search_events(item: dict, parent_uuid: str) -> list[dict]:
    tool_use_id = item.get("id") or _new_uuid()
    tool_use = _normalize_web_search(item, parent_uuid, tool_use_id)
    tool_result = _normalize_web_search_result(item, tool_use["uuid"], tool_use_id)
    return [tool_use] + ([tool_result] if tool_result else [])


def _json_obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    if value is None:
        return {}
    return {"value": value}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _parent_tool_use_id(payload: dict) -> str:
    for key in (
        "parent_tool_use_id",
        "parentToolUseId",
        "parent_call_id",
        "parentCallId",
        "parent_item_id",
        "parentItemId",
        "parent_id",
        "parentId",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _with_parent_tool_use_id(event: dict, payload: dict) -> dict:
    parent_tool_use_id = _parent_tool_use_id(payload)
    if parent_tool_use_id:
        event["parent_tool_use_id"] = parent_tool_use_id
    return event


def _attach_collab_parent_from_thread(
    item: dict,
    collab_thread_parents: dict[str, str],
) -> dict:
    item_thread_id = item.get("threadId")
    if (
        isinstance(item_thread_id, str)
        and item_thread_id in collab_thread_parents
        and not _parent_tool_use_id(item)
    ):
        return {**item, "parentToolUseId": collab_thread_parents[item_thread_id]}
    return item


def _remember_collab_receivers(item: dict, collab_thread_parents: dict[str, str]) -> None:
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return
    receivers = item.get("receiverThreadIds")
    if not isinstance(receivers, list):
        return
    for receiver in receivers:
        if isinstance(receiver, str) and receiver:
            collab_thread_parents[receiver] = item_id


def _normalize_agent_args(args: dict) -> dict:
    prompt = _first_text(
        args.get("prompt"),
        args.get("message"),
        args.get("task"),
        args.get("description"),
    )
    description = _first_text(
        args.get("description"),
        args.get("task"),
        args.get("prompt"),
        args.get("message"),
    )
    subagent_type = _first_text(
        args.get("agent_type"),
        args.get("subagent_type"),
        args.get("type"),
        args.get("name"),
    ) or "default"
    normalized = dict(args)
    normalized["subagent_type"] = subagent_type
    if description:
        normalized["description"] = description
    if prompt:
        normalized["prompt"] = prompt
    return normalized


def _response_text_content(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _response_input_text_content(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _extract_subagent_notification(text: str) -> Optional[dict]:
    start = text.find("<subagent_notification>")
    end = text.find("</subagent_notification>")
    if start < 0 or end < 0 or end <= start:
        return None
    raw = text[start + len("<subagent_notification>"):end].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_subagent_notification(payload: dict, parent_uuid: str) -> Optional[dict]:
    notification = _extract_subagent_notification(_response_input_text_content(payload))
    if notification is None:
        return None
    agent_path = notification.get("agent_path")
    status = notification.get("status")
    content = status
    if not isinstance(content, str):
        content = json.dumps(status, ensure_ascii=False, default=str)
    return _with_parent_tool_use_id({
        "type": "user",
        "codex_subagent_id": str(agent_path or ""),
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": str(agent_path or ""),
                "content": content,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_response_message(payload: dict, parent_uuid: str) -> Optional[dict]:
    # Native Codex session files include developer/user history as
    # response_item.message records. Better Agent already owns user
    # message scaffolds, so only assistant output becomes render events.
    if payload.get("role") != "assistant":
        return _normalize_subagent_notification(payload, parent_uuid)
    text = _response_text_content(payload)
    if not text:
        return None
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "codex",
        },
        "uuid": _response_item_uuid(parent_uuid, payload),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_response_reasoning(payload: dict, parent_uuid: str) -> Optional[dict]:
    summary = payload.get("summary")
    if not isinstance(summary, list) or not summary:
        # Encrypted-only or absent reasoning has no renderable content.
        return None
    parts: list[str] = []
    for block in summary:
        if isinstance(block, dict):
            text = block.get("text") or block.get("summary")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    text = "\n".join(p for p in parts if p)
    if not text:
        return None
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": "codex",
        },
        "uuid": _response_item_uuid(parent_uuid, payload),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _native_payload_label(event_type: str, payload: dict) -> str:
    payload_type = payload.get("type")
    return f"{event_type}.{payload_type}" if payload_type else event_type


def _native_payload_text(event_type: str, payload: Any) -> str:
    try:
        body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except TypeError:
        body = str(payload)
    return f"Codex native {_native_payload_label(event_type, payload if isinstance(payload, dict) else {})}\n\n```json\n{body}\n```"


def _normalize_native_payload(event_type: str, payload: Any, parent_uuid: str) -> dict:
    event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": _native_payload_text(event_type, payload)}],
            "model": "codex",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }
    if isinstance(payload, dict):
        return _with_parent_tool_use_id(event, payload)
    return event


def _normalize_event_msg_text(payload: dict, parent_uuid: str, text: str) -> dict:
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "codex",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_event_msg_reasoning(payload: dict, parent_uuid: str, text: str) -> dict:
    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
            "model": "codex",
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload)


def _normalize_event_msg_patch_apply_end(payload: dict, parent_uuid: str) -> dict:
    output = payload.get("stdout") or payload.get("stderr") or ""
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, default=str)
    if payload.get("success") is False and output:
        output = f"Patch failed\n{output}"
    elif not output:
        output = "Patch applied" if payload.get("success") else "Patch finished"
    event, _ = _normalize_response_tool_result(
        {
            "type": "custom_tool_call_output",
            "call_id": payload.get("call_id") or payload.get("id") or "",
            "output": output,
        },
        parent_uuid,
    )
    return event


def _duration_text(duration_ms: object) -> Optional[str]:
    if not isinstance(duration_ms, int) or duration_ms < 1000:
        return None
    total_seconds = round(duration_ms / 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _event_msg_notice_text(payload: dict) -> str:
    payload_type = payload.get("type")
    if payload_type == "context_compacted":
        return "Context compacted"
    if payload_type == "turn_aborted":
        reason = payload.get("reason")
        text = "Turn interrupted" if reason == "interrupted" else "Turn aborted"
        duration = _duration_text(payload.get("duration_ms"))
        return f"{text} after {duration}" if duration else text
    return "Codex event"


def _normalize_event_msg_notice(payload: dict, parent_uuid: str) -> dict:
    return _with_parent_tool_use_id({
        "type": "lifecycle_notice",
        "data": {
            "kind": payload.get("type") or "codex_event",
            "message": _event_msg_notice_text(payload),
            "reason": payload.get("reason"),
            "duration_ms": payload.get("duration_ms"),
            "timestamp": datetime.now().isoformat(),
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
    }, payload)


def _normalize_sub_agent_activity(payload: dict, parent_uuid: str) -> dict:
    agent_path = str(payload.get("agent_path") or payload.get("agent") or "sub-agent")
    kind = str(payload.get("kind") or "updated")
    return _with_parent_tool_use_id({
        "type": "lifecycle_notice",
        "data": {
            "kind": "sub_agent_activity",
            "message": f"Sub-agent {agent_path} {kind}",
            "agent_path": agent_path,
            "status": kind,
            "event_id": payload.get("event_id"),
            "timestamp": datetime.now().isoformat(),
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
    }, payload)


def _codex_agent_message_parts(payload: dict) -> tuple[str, str]:
    text = _response_input_text_content(payload)
    if not text:
        return "", ""
    message_type = ""
    for line in text.splitlines():
        prefix = "Message Type:"
        if line.startswith(prefix):
            message_type = line[len(prefix):].strip()
            break
    marker = "Payload:"
    if marker not in text:
        return message_type, text.strip()
    return message_type, text.split(marker, 1)[1].strip()


def _normalize_response_tool_call(payload: dict, parent_uuid: str) -> tuple[dict, str]:
    tool_use_id = payload.get("call_id") or payload.get("id") or _new_uuid()
    payload_type = payload.get("type")
    name = payload.get("name") or "unknown"
    args = _json_obj(payload.get("arguments", payload.get("input")))

    if payload_type == "tool_search_call":
        name = "tool_search_tool"

    if name == "exec_command":
        name = "Bash"
        if "cmd" in args and "command" not in args:
            args = {**args, "command": args["cmd"]}
    elif name in _CODEX_AGENT_TOOL_NAMES:
        name = "Agent"
        args = _normalize_agent_args(args)
    elif name == "update_plan":
        # Codex's native planning tool: `plan: [{step, status}]`. Map to
        # Claude's TodoWrite shape so the Todos extension reconstructs it as
        # `current_todos` — same boundary-normalization pattern as
        # `_normalize_todo_list` (Codex stream item) and Gemini's
        # update_topic→TodoWrite rename. Status vocabulary is identical to
        # TodoWrite (pending/in_progress/completed), so it passes through.
        name, args = "TodoWrite", _codex_update_plan_to_todos(args)

    return _with_parent_tool_use_id({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": name,
                "input": args,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload), tool_use_id


def _codex_update_plan_to_todos(args: dict) -> dict:
    """Build a Claude `TodoWrite` input (`{"todos": [...]}`) from a Codex
    `update_plan` tool_call payload (`{"plan": [{"step","status"}]}`).

    `step` → `content`, `status` → `status` (Codex already uses the same
    pending/in_progress/completed vocabulary as TodoWrite). The optional
    `explanation` has no TodoWrite slot and is dropped.
    """
    raw_plan = args.get("plan") if isinstance(args, dict) else None
    todos: list[dict] = []
    if isinstance(raw_plan, list):
        for entry in raw_plan:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if not isinstance(status, str) or not status:
                status = "pending"
            todos.append({
                "content": str(entry.get("step", "") or ""),
                "status": status,
                "activeForm": None,
            })
    return {"todos": todos}


def _normalize_response_tool_result(payload: dict, parent_uuid: str) -> tuple[dict, str]:
    tool_use_id = payload.get("call_id") or payload.get("id") or ""
    output: Any = payload.get("output", "")
    if output == "":
        for key in ("result", "content", "tools"):
            if key in payload:
                output = payload.get(key)
                break
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    return _with_parent_tool_use_id({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": output,
            }],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }, payload), tool_use_id

def _normalize_item(payload: dict, parent_uuid: str, provider: Optional[Any] = None) -> dict:
    # ... inside payload_type checks for tool outputs ...
    if payload_type in (
        "function_call_output",
        "custom_tool_call_output",
        "tool_search_output",
    ):
        event, _ = _normalize_response_tool_result(payload, parent_uuid)
        if provider:
            return provider.format_tool_result(event["tool_use_id"], event["content"])
        return event
    # ...


def _web_search_item_from_payload(payload: dict) -> dict:
    action = payload.get("action") or {}
    query = payload.get("query") or ""
    if not query and isinstance(action, dict):
        query = action.get("query") or action.get("url") or ""
    return {
        "id": payload.get("call_id") or payload.get("id") or _new_uuid(),
        "query": query,
        "action": action,
    }


def _web_search_dedupe_key(item: dict) -> str:
    return json.dumps(
        {"query": item.get("query", ""), "action": item.get("action", "")},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _normalize_response_item_event(payload: dict, parent_uuid: str) -> Optional[dict]:
    payload_type = payload.get("type")
    if payload_type == "message":
        return _normalize_response_message(payload, parent_uuid)
    if payload_type == "reasoning":
        return _normalize_response_reasoning(payload, parent_uuid)
    if payload_type in ("function_call", "custom_tool_call", "tool_search_call"):
        event, _ = _normalize_response_tool_call(payload, parent_uuid)
        event["uuid"] = _response_item_uuid(parent_uuid, payload, ":tool_use")
        return event
    if payload_type in (
        "function_call_output",
        "custom_tool_call_output",
        "tool_search_output",
    ):
        event, _ = _normalize_response_tool_result(payload, parent_uuid)
        event["uuid"] = _response_item_uuid(parent_uuid, payload, ":tool_result")
        return event
    if payload_type == "web_search_call":
        event = _normalize_web_search(_web_search_item_from_payload(payload), parent_uuid)
        event["uuid"] = _response_item_uuid(parent_uuid, payload, ":web_search")
        return event
    event = _normalize_native_payload("response_item", payload, parent_uuid)
    event["uuid"] = _response_item_uuid(parent_uuid, payload, ":native")
    return event


def _normalize_error_item(item: dict, parent_uuid: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"Error: {item.get('message', 'unknown error')}"}],
        },
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
        "isStreamError": True,
    }


def _normalize_todo_list(
    item: dict, parent_uuid: str, event_uuid: str,
) -> Optional[dict]:
    """Normalize a Codex `todo_list` stream item to a Claude-shaped
    `TodoWrite` tool_use event (full-list snapshot → REPLACE semantics).

    Codex emits a single todo_list item (stable `id`, e.g. `item_0`)
    that mutates in place across item.started → item.updated →
    item.completed; each emission carries the WHOLE list with a binary
    `completed` flag per entry. Mapping to Claude's `TodoWrite` todos[]
    lets the Todos extension REPLACE `current_todos` on every emission.
    `event_uuid` is stable per (run, item id) so `apply_event` REPLACEs
    the render-tree node rather than appending a card per update.

    Status: Codex's stream has no `in_progress` — the FIRST
    not-completed entry is surfaced as `in_progress` (matches Codex's
    own TUI active-step rendering and the Gemini in_progress heuristic);
    remaining not-completed → `pending`; completed → `completed`.
    """
    raw_items = item.get("items")
    if not isinstance(raw_items, list):
        return None
    todos: list[dict] = []
    in_progress_assigned = False
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        if entry.get("completed"):
            status = "completed"
        elif not in_progress_assigned:
            status = "in_progress"
            in_progress_assigned = True
        else:
            status = "pending"
        todos.append({
            "content": entry.get("text") or "",
            "status": status,
            "activeForm": None,
        })
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": item.get("id") or _new_uuid(),
                "name": "TodoWrite",
                "input": {"todos": todos},
            }],
        },
        "uuid": event_uuid,
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }
