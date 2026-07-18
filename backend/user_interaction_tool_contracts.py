from __future__ import annotations

from typing import Any


REQUEST_USER_APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "The concrete action or decision that needs approval.",
        },
        "timeout_seconds": {
            "type": "number",
            "description": "Optional wait timeout, 1-86400 seconds. Default 86400.",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

REQUEST_USER_APPROVAL_DESCRIPTION = (
    "Ask the user to approve one concrete action and wait for their decision. "
    "The user can either approve or provide alternative instructions. Use this "
    "only when explicit approval is required before continuing. Returns "
    "approved=true, or approved=false with the user's alternative text."
)
