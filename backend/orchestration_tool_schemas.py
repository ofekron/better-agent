from __future__ import annotations

from typing import Any


DELEGATE_TASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The task to hand off. Routed automatically unless target_session_id is set.",
        },
        "target_session_id": {
            "type": "string",
            "description": (
                "OPTIONAL - set ONLY to bypass auto-routing and send to a "
                "specific session. Omit to let the router pick (search or create)."
            ),
        },
        "provider_id": {
            "type": "string",
            "description": "OPTIONAL - provider for auto-routing search and newly-created target sessions. Defaults to the caller's provider; use ANY to search across providers.",
        },
        "model": {
            "type": "string",
            "description": "OPTIONAL - model for a newly-created target session. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "OPTIONAL - reasoning effort for a newly-created target session. Defaults to the creating session's effort.",
        },
        "sub_session": {
            "type": "boolean",
            "description": "OPTIONAL - default true. If false, auto-created targets are standalone native sessions instead of hidden sub-sessions.",
        },
        "cwd": {
            "type": "string",
            "description": "OPTIONAL - working directory for a newly-created target session. Defaults to (inherits) the creating session's cwd. Ignored when delegating to an existing target_session_id.",
        },
    },
    "required": ["task"],
}


ENSURE_NAMED_WORKER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Stable worker name. Matched (case-sensitive) as the session "
                "name `worker:<name>` together with cwd — that (name, cwd) "
                "pair is the singleton key. Reuses the existing worker if "
                "present, otherwise creates one."
            ),
        },
        "cwd": {
            "type": "string",
            "description": (
                "OPTIONAL - registry cwd for the worker. Together with name it "
                "identifies the singleton. Defaults to (inherits) the creating "
                "session's cwd; set it only to target a different project root."
            ),
        },
        "orchestration_mode": {
            "type": "string",
            "enum": ["team", "native"],
            "description": (
                "'native' = a plain session that does work directly. 'team' = "
                "a sub-coordinator that can itself delegate to workers."
            ),
        },
        "provision_prompt": {
            "type": "string",
            "description": (
                "OPTIONAL first-turn prompt applied ONLY on first creation "
                "(ignored when reusing an existing worker). Use to seed the "
                "worker's role/expertise."
            ),
        },
        "description": {
            "type": "string",
            "description": "OPTIONAL human-readable description; defaults to `worker:<name>`.",
        },
        "provider_id": {
            "type": "string",
            "description": "OPTIONAL - provider for the worker. Defaults to the creating session's provider.",
        },
        "model": {
            "type": "string",
            "description": "OPTIONAL - model for the worker. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "OPTIONAL - reasoning effort for the worker. Defaults to the creating session's effort.",
        },
        "node_id": {
            "type": "string",
            "description": "OPTIONAL - worker node id. Defaults to the session's node_id.",
        },
    },
    "required": ["name", "orchestration_mode"],
}


LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "description": "Optional fuzzy provider id, name, or kind filter.",
        },
        "model": {
            "type": "string",
            "description": "Optional fuzzy model filter.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "Optional fuzzy reasoning effort filter.",
        },
    },
}
