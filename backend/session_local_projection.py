from __future__ import annotations

from typing import Any

import perf
from todo_projection import extract_tasks_from_normalized, extract_todos_from_normalized


_ALL_TASKS_DONE_MARKER_TAG = "ALL_TASKS__DONE"


def _agent_message_text(normalized: dict[str, Any]) -> str:
    data = normalized.get("data") or {}
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _all_tasks_done_items(items: list) -> list | None:
    if not items:
        return None
    return [
        {**item, "status": "completed"}
        for item in items
        if isinstance(item, dict)
    ]


def project_event_fields(normalized: dict[str, Any], current_todos: list, current_tasks: list) -> dict[str, list]:
    with perf.timed("session.local_projection.project_event"):
        fields: dict[str, list] = {}
        todos = extract_todos_from_normalized(normalized, current_todos)
        if todos is not None:
            fields["current_todos"] = todos
        elif f"<{_ALL_TASKS_DONE_MARKER_TAG}>" in _agent_message_text(normalized):
            completed = _all_tasks_done_items(current_todos)
            if completed is not None:
                fields["current_todos"] = completed
        tasks = extract_tasks_from_normalized(normalized, current_tasks)
        if tasks is not None:
            fields["current_tasks"] = tasks
        elif f"<{_ALL_TASKS_DONE_MARKER_TAG}>" in _agent_message_text(normalized):
            completed_tasks = _all_tasks_done_items(current_tasks)
            if completed_tasks is not None:
                fields["current_tasks"] = completed_tasks
        return fields
