"""Strict, fail-closed validation for the A2A AgentCard shape.

Validates only the fields Better Agent actually reads. Unknown extra
fields on the card are preserved (forward-compatible with newer spec
minor versions) but every field this module reads is type-checked;
anything missing or mistyped raises `AgentCardValidationError` rather
than being coerced or defaulted.
"""
from __future__ import annotations

_REQUIRED_STRING_FIELDS = ("name", "description", "url", "version")


class AgentCardValidationError(ValueError):
    pass


def _require_str(card: dict, field: str) -> None:
    value = card.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AgentCardValidationError(f"agent card field {field!r} must be a non-empty string")


def _require_str_list(card: dict, field: str) -> None:
    value = card.get(field)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AgentCardValidationError(f"agent card field {field!r} must be a list of strings")


def validate_agent_card(data: object) -> dict:
    """Validate a fetched agent-card payload. Returns the same dict on
    success. Raises AgentCardValidationError on any shape violation —
    callers must never persist or act on an unvalidated card."""
    if not isinstance(data, dict):
        raise AgentCardValidationError("agent card must be a JSON object")
    for field in _REQUIRED_STRING_FIELDS:
        _require_str(data, field)
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, dict):
        raise AgentCardValidationError("agent card field 'capabilities' must be an object")
    _require_str_list(data, "defaultInputModes")
    _require_str_list(data, "defaultOutputModes")
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise AgentCardValidationError("agent card field 'skills' must be a list")
    for index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            raise AgentCardValidationError(f"agent card skills[{index}] must be an object")
        for field in ("id", "name", "description"):
            value = skill.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AgentCardValidationError(
                    f"agent card skills[{index}].{field} must be a non-empty string"
                )
        tags = skill.get("tags")
        if tags is not None and not (
            isinstance(tags, list) and all(isinstance(t, str) for t in tags)
        ):
            raise AgentCardValidationError(f"agent card skills[{index}].tags must be a list of strings")
    return data
