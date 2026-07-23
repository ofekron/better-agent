"""Named capability presets applied at session creation.

A preset is a creation-time template that resolves to the session
record's capability-exclusion fields; after creation the record fields
remain the single source of truth. Presets can only REMOVE capabilities
— they never grant anything — so applying one is always a narrowing
operation and unknown preset names fail closed.
"""

from __future__ import annotations

import extension_store

REVIEWER_PRESET = "reviewer"

# The reviewer keeps `mssg` (its async reply channel to the caller) and
# loses every capability that can reach, create, or wait on another
# session, plus all runtime skills (including command-adv itself).
_PRESETS: dict[str, dict[str, list[str]]] = {
    REVIEWER_PRESET: {
        "disabled_builtin_tools": [
            "ask",
            "create_session",
            "create_sub_session",
            "delegate_task",
        ],
        "disabled_builtin_extensions": [
            extension_store.BUILTIN_SESSION_BRIDGE_EXTENSION_ID,
        ],
        "disabled_runtime_skills": ["*"],
    },
}

PRESET_NAMES = tuple(sorted(_PRESETS))

_LIST_FIELDS = (
    "disabled_builtin_tools",
    "disabled_builtin_extensions",
    "disabled_runtime_skills",
)


def normalize_preset(value: object) -> str:
    name = str(value or "").strip().lower()
    if not name:
        return ""
    if name not in _PRESETS:
        raise ValueError(
            f"unknown session preset {name!r}; valid presets: "
            f"{', '.join(PRESET_NAMES)}"
        )
    return name


def apply_preset(preset: str, fields: dict) -> dict:
    """Merge `preset`'s exclusions into creation kwargs `fields` (union
    with any caller-supplied exclusion lists) and return `fields`."""
    name = normalize_preset(preset)
    if not name:
        return fields
    for key in _LIST_FIELDS:
        extra = _PRESETS[name].get(key) or []
        if not extra:
            continue
        current = fields.get(key) or []
        fields[key] = list(dict.fromkeys([*current, *extra]))
    return fields
