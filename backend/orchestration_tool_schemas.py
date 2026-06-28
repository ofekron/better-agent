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
            "type": ["string", "null"],
            "description": (
                "OPTIONAL - set ONLY to bypass auto-routing and send to a "
                "specific session. Omit to let the router pick (search or create)."
            ),
        },
        "provider_id": {
            "type": ["string", "null"],
            "description": "OPTIONAL - provider for a newly-created target session. Defaults to the creating session's provider.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "OPTIONAL - model for a newly-created target session. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": ["string", "null"],
            "description": "OPTIONAL - reasoning effort for a newly-created target session. Defaults to the creating session's effort.",
        },
        "sub_session": {
            "type": "boolean",
            "description": "OPTIONAL - default true. If false, auto-created targets are standalone native sessions instead of hidden sub-sessions.",
        },
    },
    "required": ["task"],
}
