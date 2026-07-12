from __future__ import annotations

from typing import Any

USER_INPUT_MAX_QUESTIONS = 20
USER_INPUT_MAX_OPTIONS = 3


def build_request_user_input_schema(*, additional_properties: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": USER_INPUT_MAX_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "header": {"type": "string"},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "maxItems": USER_INPUT_MAX_OPTIONS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                            },
                        },
                    },
                    "required": ["id", "header", "question"],
                },
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Optional wait timeout, 1-86400 seconds. Default 86400.",
            },
        },
        "required": ["questions"],
    }
    if additional_properties is not None:
        schema["additionalProperties"] = additional_properties
    return schema
