"""Routes a harness field write to the right home.

Global-scoped fields always write through to their owning store. On the
Default profile every field writes through, keeping Default a live
projection rather than a second copy. On a named profile, profile-scoped
fields become sparse overrides instead.

Item toggles arrive absolute ("this skill is on"); stored overrides are
deltas relative to Default. The conversion lives here so no caller ever
hand-authors a delta.
"""

from __future__ import annotations

from typing import Any

import extension_store
import harness_fields
import harness_profile_resolver
import harness_profile_store
from harness_fields import HarnessFieldError


def _toggled(current: list[str], name: str, present: bool) -> list[str]:
    entries = [item for item in current if item != name]
    if present:
        entries.append(name)
    return entries


def apply_field_writes(
    profile_id: str, revision: str | None, writes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Apply every write, then return the stored profile record (``None``
    for Default, which has no stored record)."""
    is_default = profile_id == harness_profile_store.DEFAULT_PROFILE_ID
    default = harness_profile_resolver.compute_default_profile()
    resolved = harness_profile_resolver.resolve_profile(profile_id, revision, default=default)

    ops: list[dict[str, Any]] = []
    # Working copies of the absolute resolved lists, mutated across the
    # writes in one request so two toggles on the same leaf compose.
    lists: dict[tuple[str, ...], list[str]] = {}

    def leaf_list(key: tuple[str, ...], current: list[str]) -> list[str]:
        if key not in lists:
            lists[key] = list(current)
        return lists[key]

    for write in writes:
        path = [str(part) for part in (write.get("path") or [])]
        value = write.get("value")
        if is_default or harness_fields.scope_for(path) == harness_fields.SCOPE_GLOBAL:
            harness_fields.write_default(path, value)
            continue

        head = path[0]
        if head in (
            harness_fields.GROUP_DISABLED_BUILTIN_TOOLS,
            harness_fields.GROUP_DISABLED_BUILTIN_EXTENSIONS,
        ):
            # The toggle reads "available"; the stored field lists disables.
            key = (head,)
            lists[key] = _toggled(leaf_list(key, resolved[head]["resolved"]), path[1], not bool(value))
            ops.append({
                "path": [head],
                "op": "set",
                "value": harness_fields.delta_for(default[head], lists[key]),
            })
            continue

        extension_id, group = path[1], path[2]
        instance = (resolved["extension_instances"] or {}).get(extension_id)
        if instance is None:
            raise HarnessFieldError(f"Extension is not active: {extension_id}")

        if group == harness_fields.GROUP_USER_INSTRUCTIONS:
            name = harness_fields.user_instruction_source_name(extension_id)
            text = str(value or "").strip()
            ops.append(
                {"path": ["instruction_sources", name], "op": "clear"}
                if not text
                else {
                    "path": ["instruction_sources", name],
                    "op": "set",
                    "value": {"kind": "inline", "content": text},
                }
            )
            continue

        if group == harness_fields.GROUP_SETTINGS:
            record = extension_store.get_extension(extension_id)
            if record is None:
                raise HarnessFieldError(f"Extension not installed: {extension_id}")
            ops.append({
                "path": ["extension_instances", extension_id, group, path[3]],
                "op": "set",
                "value": {
                    "value": value,
                    "schema_hash": harness_fields.setting_schema_hash(record, path[3]),
                },
            })
            continue

        key = (extension_id, group)
        lists[key] = _toggled(leaf_list(key, instance[group]["resolved"]), path[3], bool(value))
        ops.append({
            "path": ["extension_instances", extension_id, group],
            "op": "set",
            "value": harness_fields.delta_for(
                (default["extension_instances"].get(extension_id) or {}).get(group) or [],
                lists[key],
            ),
        })

    if is_default:
        return None
    if ops:
        return harness_profile_store.apply_override_patch(profile_id, ops, revision)
    return harness_profile_store.get_profile(profile_id)
