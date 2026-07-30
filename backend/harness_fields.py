"""Declarative registry of everything a harness profile can configure.

One source of truth for three consumers that used to hardcode the field set
independently: the Default synthesis in `harness_profile_resolver`, the
profile write endpoints in `main.py`, and the settings UI.

Each field carries its scope:

* ``SCOPE_PROFILE`` — a named profile may override it; editing it on Default
  writes through to the owning store (`extension_store` / `config_store`).
* ``SCOPE_GLOBAL`` — install/consent/chrome state that a profile must never
  be able to rebind (permission grants, secrets, UI surfaces, native
  ambient exposure). Editing it from any profile writes the one global
  value. Profiles carry secret *references* only, never secret values.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import config_store
import extension_descriptions
import extension_instructions
import extension_store


class HarnessFieldError(ValueError):
    pass


SCOPE_PROFILE = "profile"
SCOPE_GLOBAL = "global"

# How the UI renders a group. `item_toggles` is a list of independently
# toggleable named items; `settings` is a manifest-declared key/value form;
# `text` is a single free-text value; `instructions` is the one group whose
# granularity differs between Default (extension-level) and named profiles
# (per-section) — see GROUP_INSTRUCTIONS below.
CONTROL_ITEM_TOGGLES = "item_toggles"
CONTROL_SETTINGS = "settings"
CONTROL_TEXT = "text"
CONTROL_INSTRUCTIONS = "instructions"

GROUP_MCP = "mcp_servers"
GROUP_SKILLS = "skills"
GROUP_INSTRUCTIONS = "instruction_names"
GROUP_SETTINGS = "setting_overlays"
GROUP_USER_INSTRUCTIONS = "user_instructions"
GROUP_NATIVE_EXPOSURE = "native_exposure"
GROUP_FRONTEND_MODULES = "frontend_modules"
GROUP_UI_SURFACES = "ui_surfaces"
GROUP_PERMISSIONS = "permissions"

# Top-level (non per-extension) groups.
GROUP_DISABLED_BUILTIN_TOOLS = "disabled_builtin_tools"
GROUP_DISABLED_BUILTIN_EXTENSIONS = "disabled_builtin_extensions"
GROUP_DISABLED_RUNTIME_SKILLS = "disabled_runtime_skills"

# Profile-meta: scalar profile fields that are NOT sparse deltas over Default
# and do not exist on the Default profile — the optional base-profile pointer,
# provider/model/reasoning-effort pins, and provisioning prompt. Written
# through the same /fields route (see harness_field_writer), stored by
# set_profile_meta.
GROUP_PROFILE_META = "profile_meta"
BASE_PROFILE_FIELD = "base_profile_id"
PIN_FIELDS = ("default_runtime_profile_id", "default_model", "default_reasoning_effort")
PROVISIONING_PROMPT_FIELD = "provisioning_prompt"
PROFILE_META_FIELDS = (BASE_PROFILE_FIELD, *PIN_FIELDS, PROVISIONING_PROMPT_FIELD)

_UI_SURFACE_KEYS = ("quick_button", "page")


def setting_schema_hash(record: dict[str, Any], key: str) -> str:
    """Stable short hash of a setting's manifest declaration.

    A stored profile pins this alongside an overridden value so a later
    manifest change (type/enum narrowing) is caught at resolve time instead
    of silently feeding an incompatible value to the extension.
    """
    for item in (record.get("manifest") or {}).get("entrypoints", {}).get("settings") or []:
        if isinstance(item, dict) and item.get("key") == key:
            blob = json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:16]
    raise HarnessFieldError(f"Unknown extension setting: {(record.get('manifest') or {}).get('id')}.{key}")


_USER_INSTRUCTION_SUFFIX = " user instructions"


def user_instruction_source_name(extension_id: str) -> str:
    """Instruction-source key under which an extension's free-text user
    instructions appear. Must match `harness_profile_resolver`'s Default
    synthesis so a named profile's inline override lands on the same key."""
    return f"{extension_id}{_USER_INSTRUCTION_SUFFIX}"


def user_instruction_source_owner(name: str) -> str | None:
    """Inverse of `user_instruction_source_name`: the extension owning a
    free-text user-instruction source key, or None for any other key."""
    if not name.endswith(_USER_INSTRUCTION_SUFFIX):
        return None
    return name[: -len(_USER_INSTRUCTION_SUFFIX)] or None


def delta_for(default_list: list[str], desired: list[str]) -> dict[str, list[str]]:
    """Minimal `{add, remove}` delta taking `default_list` to `desired`.

    Item toggles in the UI are absolute ("this skill is on"); the stored
    override is relative to Default. Deriving the delta here keeps the two
    representations from drifting and means a user never hand-authors one.
    """
    desired_set = list(dict.fromkeys(desired))
    return {
        "add": [item for item in desired_set if item not in default_list],
        "remove": [item for item in default_list if item not in desired_set],
    }


def _group(
    group_id: str,
    *,
    scope: str,
    control: str,
    items: list[dict[str, Any]] | None = None,
    value: Any = None,
) -> dict[str, Any]:
    return {
        "id": group_id,
        "scope": scope,
        "control": control,
        "items": items or [],
        "value": value,
    }


def _mcp_group(extension_id: str) -> dict[str, Any]:
    items = []
    for server in extension_store.extension_mcp_servers(extension_id):
        forced = list(server.get("forced_by_skills") or [])
        items.append({
            "name": server["name"],
            "label": server.get("label") or server["name"],
            "description": server.get("description") or "",
            "description_path": server.get("description_path") or "",
            "detail": "",
            "default_enabled": bool(server.get("enabled")),
            # A server an enabled skill depends on cannot be turned off
            # without first disabling that skill — surface the reason rather
            # than letting the write fail after the click.
            "locked_by": forced,
        })
    return _group(GROUP_MCP, scope=SCOPE_PROFILE, control=CONTROL_ITEM_TOGGLES, items=items)


def _skills_group(extension_id: str) -> dict[str, Any]:
    items = [
        {
            "name": skill["name"],
            "label": skill["name"],
            "description": skill.get("description") or "",
            "description_path": skill.get("description_path") or "",
            "detail": "",
            "default_enabled": bool(skill.get("enabled")),
            "locked_by": [],
        }
        for skill in extension_store.extension_runtime_skills(extension_id)
    ]
    return _group(GROUP_SKILLS, scope=SCOPE_PROFILE, control=CONTROL_ITEM_TOGGLES, items=items)


def _instructions_group(record: dict[str, Any]) -> dict[str, Any]:
    entrypoints = (record.get("manifest") or {}).get("entrypoints") or {}
    state = extension_instructions.normalize_state(record)
    enabled = bool(state.get("global"))
    root = extension_store.runtime_package_root_for_record(record)
    items = [
        {
            "name": str(item["name"]),
            "label": str(item["name"]),
            "description": extension_descriptions.instruction_description(root, item),
            "description_path": extension_descriptions.instruction_description_path(root, item),
            "detail": "project" if item.get("level") == "project" else "global",
            # Manifest provider scope — the run path and the file-sync path
            # both drop this section for any provider outside the list, so the
            # editor must show that a toggle here is provider-limited.
            "providers": list(item.get("providers") or []),
            # Default injects either all of an extension's sections or none —
            # the store's toggle is extension-level. Named profiles select
            # per section, so per-item default state mirrors the extension flag.
            "default_enabled": enabled,
            "locked_by": [],
        }
        for item in extension_instructions.instruction_items_from_entrypoints(entrypoints) or []
        if isinstance(item, dict) and item.get("name")
    ]
    group = _group(
        GROUP_INSTRUCTIONS,
        scope=SCOPE_PROFILE,
        control=CONTROL_INSTRUCTIONS,
        items=items,
        value=enabled if items else None,
    )
    # Editing Default flips one extension-level flag; a named profile edits
    # individual sections. The UI reads this instead of hardcoding the split.
    group["default_granularity"] = "extension"
    group["profile_granularity"] = "item"
    return group


def _settings_group(extension_id: str) -> dict[str, Any]:
    record = extension_store.get_extension(extension_id)
    root = extension_store.runtime_package_root_for_record(record)
    manifest_path = str((root / "better-agent-extension.json").resolve()) if root else ""
    settings = extension_store.get_extension_settings(extension_id)
    values = settings.get("values") or {}
    secret_present = settings.get("secret_present") or {}
    items = []
    for spec in settings.get("schema") or []:
        if not isinstance(spec, dict) or not spec.get("key"):
            continue
        # Settings bound to an app Settings section hold one app-wide value
        # and are edited there, so they are never profile overlays.
        if spec.get("section"):
            continue
        key = str(spec["key"])
        is_secret = spec.get("type") == "secret"
        items.append({
            "name": key,
            "label": spec.get("label") or key,
            "description": spec.get("description") or "",
            "description_path": manifest_path if spec.get("description") else "",
            "detail": "",
            "type": spec.get("type"),
            "enum": list(spec.get("enum") or []),
            # Secrets live in the OS keychain and are never profile-scoped:
            # a profile that could rebind one would be a credential-theft
            # primitive. Presence only, always global.
            "scope": SCOPE_GLOBAL if is_secret else SCOPE_PROFILE,
            "secret": is_secret,
            "secret_present": bool(secret_present.get(key)) if is_secret else False,
            "default_value": None if is_secret else values.get(key),
            "schema_hash": "" if is_secret else setting_schema_hash(record, key),
        })
    return _group(GROUP_SETTINGS, scope=SCOPE_PROFILE, control=CONTROL_SETTINGS, items=items)


def _user_instructions_group(extension_id: str) -> dict[str, Any]:
    return _group(
        GROUP_USER_INSTRUCTIONS,
        scope=SCOPE_PROFILE,
        control=CONTROL_TEXT,
        value=extension_store.get_user_instructions(extension_id),
    )


def _native_exposure_group(record: dict[str, Any]) -> dict[str, Any]:
    items = []
    for addition in extension_store.extension_harness_additions(record):
        kind = str(addition.get("kind") or "")
        # kind="mcp" exposure moved to the scoped native-MCP grant store and
        # is no longer togglable through this flag.
        if kind == "mcp" or not addition.get("native_eligible"):
            continue
        name = str(addition.get("name") or "")
        items.append({
            "name": f"{kind}:{name}",
            "label": name,
            "description": "",
            "detail": kind,
            "default_enabled": bool(addition.get("native_exposed")),
            "locked_by": [],
        })
    return _group(GROUP_NATIVE_EXPOSURE, scope=SCOPE_GLOBAL, control=CONTROL_ITEM_TOGGLES, items=items)


def _frontend_modules_group(extension_id: str) -> dict[str, Any]:
    items = [
        {
            "name": f"{module['slot']}:{module['id']}",
            "label": module.get("label") or module["id"],
            "description": "",
            "detail": module["slot"],
            "default_enabled": bool(module.get("enabled")),
            "locked_by": [],
        }
        for module in extension_store.extension_frontend_modules(extension_id)
    ]
    return _group(GROUP_FRONTEND_MODULES, scope=SCOPE_GLOBAL, control=CONTROL_ITEM_TOGGLES, items=items)


def _ui_surfaces_group(extension_id: str, record: dict[str, Any]) -> dict[str, Any]:
    entrypoints = (record.get("manifest") or {}).get("entrypoints") or {}
    ui = extension_store.get_ui_settings(extension_id)
    available = {
        "quick_button": bool(entrypoints.get("quick_button")),
        "page": bool(entrypoints.get("page")),
    }
    items = [
        {
            "name": key,
            "label": key,
            "description": "",
            "detail": "",
            "default_enabled": bool(ui.get(f"{key}_enabled", True)),
            "locked_by": [],
        }
        for key in _UI_SURFACE_KEYS
        if available[key]
    ]
    return _group(GROUP_UI_SURFACES, scope=SCOPE_GLOBAL, control=CONTROL_ITEM_TOGGLES, items=items)


def _permissions_group(record: dict[str, Any]) -> dict[str, Any]:
    declared = extension_store.declared_permissions(record)
    grants = extension_store.permission_grants(record)
    optional = set(extension_store.optional_permissions(record))
    items = []
    for permission in sorted(declared):
        is_optional = permission in optional
        items.append({
            "name": permission,
            "label": permission,
            "description": "",
            "detail": "optional" if is_optional else "required",
            "default_enabled": bool(grants.get(permission)) if is_optional else True,
            # Required permissions are part of the install contract; only
            # optional ones are user-togglable, and never profile-scoped.
            "locked_by": [] if is_optional else ["required"],
        })
    return _group(GROUP_PERMISSIONS, scope=SCOPE_GLOBAL, control=CONTROL_ITEM_TOGGLES, items=items)


def _extension_descriptor(record: dict[str, Any], extension_id: str) -> dict[str, Any]:
    manifest = record.get("manifest") or {}
    root = extension_store.runtime_package_root_for_record(record)
    manifest_path = str((root / "better-agent-extension.json").resolve()) if root else ""
    groups = [
        _mcp_group(extension_id),
        _skills_group(extension_id),
        _instructions_group(record),
        _user_instructions_group(extension_id),
        _settings_group(extension_id),
        _native_exposure_group(record),
        _frontend_modules_group(extension_id),
        _ui_surfaces_group(extension_id, record),
        _permissions_group(record),
    ]
    return {
        "id": extension_id,
        "name": manifest.get("name") or extension_id,
        "description": manifest.get("description") or "",
        "description_path": manifest_path if manifest.get("description") else "",
        "required": extension_id in extension_store.REQUIRED_EXTENSION_IDS,
        "enabled": bool(record.get("enabled")),
        "runtime_ready": extension_store.is_extension_runtime_ready(extension_id),
        "runtime_not_ready_reason": extension_store.runtime_not_ready_reason(extension_id) or "",
        # A profile's per-extension overrides only take effect while the
        # extension is live; surface that so the UI can say so instead of
        # showing dead controls.
        "groups": [group for group in groups if group["items"] or group["value"] is not None],
    }


def _profile_meta_descriptor() -> dict[str, Any]:
    """The scalar profile-meta fields. Not a delta-over-Default control, so
    the editor renders it with a dedicated widget; option lists come from their
    own live sources, not embedded here."""
    return {
        "id": GROUP_PROFILE_META,
        "scope": SCOPE_PROFILE,
        "fields": [
            {"name": BASE_PROFILE_FIELD, "kind": "profile_ref"},
            {"name": "default_runtime_profile_id", "kind": "runtime_profile"},
            {"name": "default_model", "kind": "model"},
            {"name": "default_reasoning_effort", "kind": "reasoning_effort"},
            {"name": PROVISIONING_PROMPT_FIELD, "kind": "textarea"},
        ],
    }


def descriptor() -> dict[str, Any]:
    """Everything configurable, independent of which profile is selected.

    The UI fetches this once and renders the same control tree for Default
    and for every named profile; the per-profile values come from
    `GET /api/harness-profiles/{id}`.
    """
    extensions: list[dict[str, Any]] = []
    installed_ids: list[str] = []
    for record in extension_store.list_extensions(include_hidden=True):
        extension_id = str((record.get("manifest") or {}).get("id") or "").strip()
        if not extension_id:
            continue
        installed_ids.append(extension_id)
        extensions.append(_extension_descriptor(record, extension_id))
    extensions.sort(key=lambda item: str(item["name"]).lower())
    return {
        "extensions": extensions,
        "builtin_tools": _group(
            GROUP_DISABLED_BUILTIN_TOOLS,
            scope=SCOPE_PROFILE,
            control=CONTROL_ITEM_TOGGLES,
            items=[
                {
                    "name": tool,
                    "label": tool,
                    "description": "",
                    "detail": "",
                    # The toggle reads as "tool is available"; the stored
                    # field is the inverse (disabled list).
                    "default_enabled": tool not in config_store.get_disabled_builtin_tools(),
                    "locked_by": [],
                }
                for tool in sorted(config_store.DISABLEABLE_BUILTIN_TOOLS)
            ],
        ),
        "builtin_extensions": _group(
            GROUP_DISABLED_BUILTIN_EXTENSIONS,
            scope=SCOPE_PROFILE,
            control=CONTROL_ITEM_TOGGLES,
            items=[
                {
                    "name": extension_id,
                    "label": extension_id,
                    "description": "",
                    "detail": "",
                    "default_enabled": extension_id not in config_store.get_disabled_builtin_extensions(),
                    "locked_by": [],
                }
                for extension_id in sorted(installed_ids)
            ],
        ),
        "runtime_skills": _group(
            GROUP_DISABLED_RUNTIME_SKILLS,
            scope=SCOPE_PROFILE,
            control=CONTROL_ITEM_TOGGLES,
            items=[
                {
                    "name": "*",
                    "label": "*",
                    "description": "",
                    "detail": "",
                    "default_enabled": True,
                    "locked_by": [],
                }
            ],
        ),
        "profile_meta": _profile_meta_descriptor(),
    }


# ---------------------------------------------------------------------------
# Default write-through — editing Default mutates the owning store, so the
# live baseline stays a projection instead of becoming a second copy.
# ---------------------------------------------------------------------------
def _require_bool(value: Any, path: list[str]) -> bool:
    if not isinstance(value, bool):
        raise HarnessFieldError(f"{'.'.join(path)} must be a boolean")
    return value


def _split_native_key(key: str) -> tuple[str, str]:
    kind, separator, name = key.partition(":")
    if separator != ":" or not kind or not name:
        raise HarnessFieldError("native exposure key must be '<kind>:<name>'")
    return kind, name


def _toggle_builtin_list(current: list[str], name: str, available: bool) -> list[str]:
    entries = set(current)
    if available:
        entries.discard(name)
    else:
        entries.add(name)
    return sorted(entries)


def write_default(path: list[str], value: Any) -> None:
    """Apply one field write to the store that owns it.

    `path` uses the same shape as a stored profile's override path, so the
    caller dispatches on profile id alone and never on field identity.
    """
    if not path:
        raise HarnessFieldError("field path is required")
    head = path[0]

    if head == GROUP_DISABLED_BUILTIN_TOOLS:
        if len(path) != 2:
            raise HarnessFieldError("disabled_builtin_tools path must name one tool")
        config_store.set_disabled_builtin_tools(
            _toggle_builtin_list(config_store.get_disabled_builtin_tools(), path[1], _require_bool(value, path))
        )
        return

    if head == GROUP_DISABLED_BUILTIN_EXTENSIONS:
        if len(path) != 2:
            raise HarnessFieldError("disabled_builtin_extensions path must name one extension")
        config_store.set_disabled_builtin_extensions(
            _toggle_builtin_list(
                config_store.get_disabled_builtin_extensions(), path[1], _require_bool(value, path)
            )
        )
        return

    if head == GROUP_DISABLED_RUNTIME_SKILLS:
        raise HarnessFieldError("disabled_runtime_skills is profile-scoped only")

    if head != "extension_instances" or len(path) < 3:
        raise HarnessFieldError(f"Unknown harness field path: {'.'.join(str(p) for p in path)}")

    extension_id, group = str(path[1]), str(path[2])
    leaf = str(path[3]) if len(path) > 3 else ""

    if group == GROUP_MCP:
        extension_store.set_mcp_server_enabled(extension_id, leaf, _require_bool(value, path))
        return
    if group == GROUP_SKILLS:
        extension_store.set_runtime_skill_enabled(extension_id, leaf, _require_bool(value, path))
        return
    if group == GROUP_INSTRUCTIONS:
        # Default granularity is the extension-level injection flag.
        extension_store.set_instruction_enabled(
            extension_id, level="global", enabled=_require_bool(value, path)
        )
        return
    if group == GROUP_SETTINGS:
        extension_store.set_extension_setting(extension_id, leaf, value)
        return
    if group == GROUP_USER_INSTRUCTIONS:
        extension_store.set_user_instructions(extension_id, value)
        return
    if group == GROUP_NATIVE_EXPOSURE:
        kind, name = _split_native_key(leaf)
        extension_store.set_native_harness_exposed(extension_id, kind, name, _require_bool(value, path))
        return
    if group == GROUP_FRONTEND_MODULES:
        slot, separator, module_id = leaf.partition(":")
        if separator != ":" or not slot or not module_id:
            raise HarnessFieldError("frontend module key must be '<slot>:<module_id>'")
        extension_store.set_frontend_module_enabled(extension_id, slot, module_id, _require_bool(value, path))
        return
    if group == GROUP_UI_SURFACES:
        enabled = _require_bool(value, path)
        if leaf == "quick_button":
            extension_store.set_ui_settings(extension_id, quick_button_enabled=enabled)
            return
        if leaf == "page":
            extension_store.set_ui_settings(extension_id, page_enabled=enabled)
            return
        raise HarnessFieldError(f"Unknown UI surface: {leaf}")
    if group == GROUP_PERMISSIONS:
        extension_store.set_permission_grant(extension_id, leaf, _require_bool(value, path))
        return
    raise HarnessFieldError(f"Unknown harness field group: {group}")


# Groups a named profile may override. Everything else is global-scope and
# always writes through, whichever profile is selected.
_PROFILE_SCOPED_GROUPS = frozenset({
    GROUP_MCP,
    GROUP_SKILLS,
    GROUP_INSTRUCTIONS,
    GROUP_SETTINGS,
    GROUP_USER_INSTRUCTIONS,
})


def _setting_spec(extension_id: str, key: str) -> dict:
    record = extension_store.get_extension(extension_id)
    if record is None:
        return {}
    for item in (record.get("manifest") or {}).get("entrypoints", {}).get("settings") or []:
        if isinstance(item, dict) and item.get("key") == key:
            return item
    return {}


def _is_global_setting(extension_id: str, key: str) -> bool:
    """A setting is global when it is secret-typed (the value lives in the OS
    keychain, so a profile that could rebind it would be a credential-theft
    primitive) or bound to an app Settings section (one app-wide value)."""
    spec = _setting_spec(extension_id, key)
    return spec.get("type") == "secret" or bool(spec.get("section"))


def scope_for(path: list[str]) -> str:
    """Scope of a field path — decides whether a named profile stores an
    override or writes the one global value."""
    if not path:
        raise HarnessFieldError("field path is required")
    if path[0] in (
        GROUP_DISABLED_BUILTIN_TOOLS,
        GROUP_DISABLED_BUILTIN_EXTENSIONS,
        GROUP_DISABLED_RUNTIME_SKILLS,
    ):
        return SCOPE_PROFILE
    if path[0] == "extension_instances" and len(path) >= 3:
        group = str(path[2])
        if group not in _PROFILE_SCOPED_GROUPS:
            return SCOPE_GLOBAL
        # Secret-typed and app-section settings hold one value, so they are
        # global even though the settings group as a whole is profile-scoped.
        if group == GROUP_SETTINGS and len(path) > 3 and _is_global_setting(str(path[1]), str(path[3])):
            return SCOPE_GLOBAL
        return SCOPE_PROFILE
    raise HarnessFieldError(f"Unknown harness field path: {'.'.join(str(p) for p in path)}")
