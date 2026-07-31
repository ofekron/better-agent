from __future__ import annotations

from typing import Any, Callable

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


def _project_collection(
    fields: dict[str, list],
    key: str,
    normalized: dict[str, Any],
    current: list,
    extractor: Callable[[dict[str, Any], list], list | None],
) -> None:
    """Project one collection: take the extractor's delta, or — when the
    agent emitted the all-tasks-done marker — mark every current item done."""
    result = extractor(normalized, current)
    if result is not None:
        fields[key] = result
    elif f"<{_ALL_TASKS_DONE_MARKER_TAG}>" in _agent_message_text(normalized):
        completed = _all_tasks_done_items(current)
        if completed is not None:
            fields[key] = completed


def project_event_fields(normalized: dict[str, Any], current_todos: list, current_tasks: list) -> dict[str, list]:
    with perf.timed("session.local_projection.project_event"):
        fields: dict[str, list] = {}
        _project_collection(fields, "current_todos", normalized, current_todos, extract_todos_from_normalized)
        _project_collection(fields, "current_tasks", normalized, current_tasks, extract_tasks_from_normalized)
        return fields
