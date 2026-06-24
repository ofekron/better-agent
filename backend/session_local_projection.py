from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import perf


_EXTRACTOR_MODULE: Any | None = None


def _extractor_module() -> Any:
    global _EXTRACTOR_MODULE
    if _EXTRACTOR_MODULE is not None:
        return _EXTRACTOR_MODULE
    path = Path(__file__).resolve().parent.parent / "extensions" / "todos" / "backend" / "extractor.py"
    spec = importlib.util.spec_from_file_location("_better_agent_todos_extractor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("todos extractor module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _EXTRACTOR_MODULE = module
    return module


def project_event_fields(normalized: dict[str, Any], current_todos: list, current_tasks: list) -> dict[str, list]:
    with perf.timed("session.local_projection.project_event"):
        extractor = _extractor_module()
        fields: dict[str, list] = {}
        todos = extractor.extract_todos_from_normalized(normalized, current_todos)
        if todos is not None:
            fields["current_todos"] = todos
        tasks = extractor.extract_tasks_from_normalized(normalized, current_tasks)
        if tasks is not None:
            fields["current_tasks"] = tasks
        return fields
