from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from better_agent_sdk import BetterAgentError, Client

from todo_projection import extract_tasks_from_normalized, extract_todos_from_normalized


TODO_FIELDS = ("current_todos", "current_tasks")


def _project_fields(event: dict[str, Any], fields: dict[str, Any]) -> dict[str, list]:
    current_todos = list(fields.get("current_todos") or [])
    current_tasks = list(fields.get("current_tasks") or [])
    updated: dict[str, list] = {}

    next_todos = extract_todos_from_normalized(event, current_todos)
    if next_todos is not None and next_todos != current_todos:
        updated["current_todos"] = next_todos

    next_tasks = extract_tasks_from_normalized(event, current_tasks)
    if next_tasks is not None and next_tasks != current_tasks:
        updated["current_tasks"] = next_tasks

    return updated


def create_router(context) -> APIRouter:
    router = APIRouter()

    @router.post("/session-event")
    def session_event(body: dict[str, Any]) -> dict[str, Any]:
        event = body.get("event") or {}
        if not isinstance(event, dict):
            return {"success": False, "error": "event must be an object"}

        session_id = str(body.get("session_id") or body.get("app_session_id") or "").strip()
        provided_fields = body.get("session_fields")
        if isinstance(provided_fields, dict):
            fields = provided_fields
        else:
            client = Client(app_session_id=session_id)
            fields = client.get_session_fields(list(TODO_FIELDS), session_id=session_id).get("fields") or {}
        updated = _project_fields(event, fields)
        sdk_applied = False
        if body.get("use_sdk") is True and session_id:
            try:
                client = Client(app_session_id=session_id)
                for field, value in updated.items():
                    client.update_session_field(field, value, session_id=session_id)
                sdk_applied = bool(updated)
            except BetterAgentError:
                sdk_applied = False
        return {
            "success": True,
            "session_fields": {} if sdk_applied else updated,
            "updated_fields": sorted(updated),
            "sdk_applied": sdk_applied,
        }

    return router
