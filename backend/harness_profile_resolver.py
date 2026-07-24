from __future__ import annotations

import copy
from typing import Any

import capability_contexts as capability_contexts_mod
import config_store
import extension_instructions
import extension_store
import harness_profile_store


class HarnessProfileResolutionError(ValueError):
    pass


# browserHarness folded onto the extension identity for the browser-harness
# role (see extension_store.extension_id_for_role); no separate identity.
def _browser_harness_extension_id() -> str | None:
    return extension_store.extension_id_for_role("browser-harness")


def _extension_revision(record: dict[str, Any]) -> str:
    source = record.get("source") or {}
    return str(source.get("commit_sha") or record.get("version") or record.get("updated_at") or "")


def _setting_schema_hash(record: dict[str, Any], key: str) -> str:
    import hashlib
    import json

    for item in (record.get("manifest") or {}).get("entrypoints", {}).get("settings") or []:
        if isinstance(item, dict) and item.get("key") == key:
            return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    raise HarnessProfileResolutionError(f"Unknown extension setting: {record['manifest']['id']}.{key}")


def _runtime_ready_record(extension_id: str) -> dict[str, Any]:
    record = extension_store.get_extension(extension_id)
    if not record or not record.get("enabled") or not extension_store.is_extension_runtime_ready(extension_id):
        raise HarnessProfileResolutionError(f"Harness profile requires enabled runtime-ready extension: {extension_id}")
    return record


def _extension_items(record: dict[str, Any]) -> dict[str, set[str]]:
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    return {
        "instructions": {
            str(item.get("name") or "")
            for item in extension_instructions.instruction_items_from_entrypoints(entrypoints) or []
            if isinstance(item, dict) and item.get("name")
        },
        "skills": {
            str(item.get("name") or "")
            for item in entrypoints.get("skills") or []
            if isinstance(item, dict) and item.get("name")
        },
        "mcp": {
            str(item.get("name") or "")
            for item in extension_store.extension_mcp_servers(str(manifest.get("id") or ""))
            if isinstance(item, dict) and item.get("name")
        },
    }


# ---------------------------------------------------------------------------
# Default synthesis — aggregates LIVE state; never persisted, never cached.
# Mirrors the read logic the deleted `_package_current_harness_profile_payload`
# used to hand-assemble, and the same live reads turn_manager's no-profile
# fallback path already performs.
# ---------------------------------------------------------------------------
def compute_default_profile() -> dict[str, Any]:
    extension_instances: dict[str, Any] = {}
    for record in extension_store.list_extensions(include_hidden=True):
        manifest = record.get("manifest") or {}
        extension_id = str(manifest.get("id") or "").strip()
        if not extension_id or not extension_store.is_extension_runtime_ready(extension_id):
            continue
        instance: dict[str, Any] = {
            "mcp_servers": [
                item["name"]
                for item in extension_store.extension_mcp_servers(extension_id)
                if item.get("enabled")
            ],
            "skills": [
                item["name"]
                for item in extension_store.extension_runtime_skills(extension_id)
                if item.get("enabled")
            ],
            "instruction_names": [],
        }
        instruction_state = extension_instructions.normalize_state(record)
        if instruction_state.get("global"):
            for item in extension_instructions.instruction_items_from_entrypoints(manifest.get("entrypoints") or {}) or []:
                if isinstance(item, dict) and item.get("name"):
                    instance["instruction_names"].append(str(item["name"]))
        settings = extension_store.get_extension_settings(extension_id)
        overlays: dict[str, Any] = {}
        secret_refs: list[str] = []
        for item in settings.get("schema") or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            key = str(item["key"])
            if item.get("type") == "secret":
                if (settings.get("secret_present") or {}).get(key):
                    secret_refs.append(f"extension-setting:{extension_id}:{key}")
                continue
            overlays[key] = {
                "value": copy.deepcopy((settings.get("values") or {}).get(key)),
                "schema_hash": _setting_schema_hash(record, key),
            }
        instance["setting_overlays"] = overlays
        instance["secret_refs"] = secret_refs
        if extension_id == _browser_harness_extension_id():
            # "headless" is a normal browserHarness extension setting, written
            # via PATCH /api/extensions/{id}/settings like any other setting;
            # read it back from the same settings values instead of
            # hardcoding, so Default reflects the live setting.
            instance["headless"] = bool((settings.get("values") or {}).get("headless", True))
        extension_instances[extension_id] = instance

    instruction_sources: dict[str, Any] = {}
    for extension_id, instance in extension_instances.items():
        for name in instance["instruction_names"]:
            instruction_sources[name] = {"kind": "extension", "extension_id": extension_id}
        user_instructions = extension_store.get_user_instructions(extension_id).strip()
        if user_instructions:
            name = f"{extension_id} user instructions"
            instruction_sources[name] = {"kind": "inline", "content": user_instructions}

    return {
        "id": "default",
        "extension_instances": extension_instances,
        "disabled_builtin_tools": config_store.get_disabled_builtin_tools(),
        "disabled_builtin_extensions": config_store.get_disabled_builtin_extensions(),
        "instruction_sources": instruction_sources,
    }


def _apply_delta(default_list: list[str], delta: dict[str, Any] | None) -> tuple[list[str], bool]:
    was_overridden = delta is not None
    if not was_overridden:
        return list(default_list), False
    add = delta.get("add") or []
    remove = set(delta.get("remove") or [])
    merged = [item for item in default_list if item not in remove]
    for item in add:
        if item not in merged:
            merged.append(item)
    return merged, True


def _field(resolved: Any, override: Any | None) -> dict[str, Any]:
    return {"resolved": resolved, "override": override, "is_overridden": override is not None}


def _apply_extension_instance_override(
    default_instance: dict[str, Any], override: dict[str, Any] | None,
) -> dict[str, Any]:
    override = override or {}
    fields: dict[str, Any] = {}
    for leaf in ("mcp_servers", "skills", "instruction_names"):
        delta = override.get(leaf)
        merged, overridden = _apply_delta(default_instance.get(leaf) or [], delta)
        fields[leaf] = _field(merged, delta if overridden else None)

    default_overlays = default_instance.get("setting_overlays") or {}
    override_overlays = override.get("setting_overlays")
    overlay_fields: dict[str, Any] = {}
    for key, default_entry in default_overlays.items():
        if override_overlays and key in override_overlays:
            entry = override_overlays[key]
            overlay_fields[key] = _field(entry, entry)
        else:
            overlay_fields[key] = _field(default_entry, None)
    if override_overlays:
        for key, entry in override_overlays.items():
            if key not in overlay_fields:
                overlay_fields[key] = _field(entry, entry)
    fields["setting_overlays"] = overlay_fields

    if "headless" in override and override.get("headless") is not None:
        fields["headless"] = _field(bool(override["headless"]), bool(override["headless"]))
    else:
        fields["headless"] = _field(bool(default_instance.get("headless", False)), None)

    # secret_refs is a Default-only, non-overridable derived field (which
    # extension settings are secret-typed and currently populated).
    fields["secret_refs"] = _field(list(default_instance.get("secret_refs") or []), None)

    # extension_revision is override-only (a drift pin); Default never pins
    # a revision since it always tracks live extension state.
    pinned = override.get("extension_revision") if "extension_revision" in override else None
    fields["extension_revision"] = _field(pinned, pinned)
    return fields


def resolve_profile(
    profile_id: str, revision: str | None = None, default: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Top-level entry point for the API layer (main.py's harness-profile
    endpoints). Returns a serializable per-field {resolved, override,
    is_overridden} snapshot for `_profile_response`/`_default_profile_response`.

    `default` lets a caller resolving many profiles in one request (e.g.
    `list_harness_profiles`) pass in an already-computed Default synthesis
    instead of triggering a fresh `compute_default_profile()` per profile.
    """
    if default is None:
        default = compute_default_profile()
    if profile_id == harness_profile_store.DEFAULT_PROFILE_ID:
        stored: dict[str, Any] | None = None
        overrides: dict[str, Any] = {}
    else:
        stored = harness_profile_store.get_profile(profile_id, revision)
        if not stored:
            raise HarnessProfileResolutionError("Harness profile is missing or its pinned revision is unavailable")
        overrides = stored.get("overrides") or {}

    override_instances = overrides.get("extension_instances") or {}
    extension_instances: dict[str, Any] = {}
    for extension_id, default_instance in default["extension_instances"].items():
        extension_instances[extension_id] = _apply_extension_instance_override(
            default_instance, override_instances.get(extension_id)
        )
    # An override may reference an extension id that is not actually
    # live-enabled/runtime-ready in `default` (not installed, disabled, or
    # never existed). Such an extension is NOT part of the live base, so an
    # override on it must be a no-op for resolution purposes — it must
    # never cause the extension to appear "present" in extension_instances.
    # Presence here is a security-relevant signal (e.g. main.py's File Edit
    # and Browser Harness gates key off it), so a profile author must not be
    # able to conjure an absent extension into apparent existence merely by
    # referencing its id in an override.

    disabled_tools, tools_overridden = _apply_delta(
        default["disabled_builtin_tools"], overrides.get("disabled_builtin_tools")
    )
    disabled_extensions, extensions_overridden = _apply_delta(
        default["disabled_builtin_extensions"], overrides.get("disabled_builtin_extensions")
    )

    instruction_sources: dict[str, Any] = {}
    override_sources = overrides.get("instruction_sources") or {}
    for name, default_source in default["instruction_sources"].items():
        if name in override_sources:
            instruction_sources[name] = _field(override_sources[name], override_sources[name])
        else:
            instruction_sources[name] = _field(default_source, None)
    for name, source in override_sources.items():
        if name not in instruction_sources:
            instruction_sources[name] = _field(source, source)

    return {
        "id": profile_id,
        "revision": (stored or {}).get("revision"),
        "name": (stored or {}).get("name"),
        "description": (stored or {}).get("description"),
        "created_at": (stored or {}).get("created_at"),
        "updated_at": (stored or {}).get("updated_at"),
        "extension_instances": extension_instances,
        "disabled_builtin_tools": _field(disabled_tools, overrides.get("disabled_builtin_tools") if tools_overridden else None),
        "disabled_builtin_extensions": _field(
            disabled_extensions, overrides.get("disabled_builtin_extensions") if extensions_overridden else None
        ),
        "instruction_sources": instruction_sources,
        "mcp_overrides": copy.deepcopy((stored or {}).get("mcp_overrides") or {}),
        "skill_overrides": copy.deepcopy((stored or {}).get("skill_overrides") or {}),
        "native_harness_overrides": copy.deepcopy((stored or {}).get("native_harness_overrides") or {}),
        "provider_run_config_overlay": copy.deepcopy((stored or {}).get("provider_run_config_overlay") or {}),
        "secret_refs": copy.deepcopy((stored or {}).get("secret_refs") or {}),
        "capability_contexts": copy.deepcopy((stored or {}).get("capability_contexts") or []),
    }


def _instruction_blocks(
    instruction_sources: dict[str, dict[str, Any]], selected: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, source in instruction_sources.items():
        if name in seen:
            continue
        if source.get("kind") == "inline":
            seen.add(name)
            blocks.append({"source": "profile", "name": name, "content": source.get("content") or ""})
            continue
        extension_id = source.get("extension_id")
        if extension_id not in selected:
            raise HarnessProfileResolutionError(f"Instruction source extension is not selected: {extension_id}")
        record = selected[extension_id]["record"]
        install_root = extension_store.runtime_package_root_for_record(record)
        if install_root is None:
            raise HarnessProfileResolutionError(f"Extension package is unavailable: {extension_id}")
        for item in extension_instructions.instruction_items_from_entrypoints((record.get("manifest") or {}).get("entrypoints") or {}) or []:
            if not isinstance(item, dict) or item.get("name") != name:
                continue
            content_path = (install_root / item["path"]).resolve()
            root = install_root.resolve()
            if not content_path.is_relative_to(root) or not content_path.is_file():
                raise HarnessProfileResolutionError(f"Instruction path is unavailable: {extension_id}.{name}")
            seen.add(name)
            blocks.append({
                "source": extension_id,
                "name": name,
                "content": content_path.read_text(encoding="utf-8").strip(),
            })
            break
        else:
            raise HarnessProfileResolutionError(f"Unknown extension instruction: {extension_id}.{name}")
    return [block for block in blocks if block.get("content")]


def _provider_context_blocks(blocks: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not blocks:
        return []
    content = "\n\n".join(f"## {block['name']}\n\n{block['content']}" for block in blocks)
    return [{
        "source_id": "harness_profile:instructions",
        "capability_id": "harness_profile:instructions",
        "name": "Harness Profile Instructions",
        "category": "harness_profile",
        "outputs": [
            {
                "provider_kind": kind,
                "provider_name": "",
                "content_kind": "instructions",
                "content": content,
            }
            for kind in ("claude", "codex", "gemini", "openai", "qwen", "kimi", "pi", "remote")
        ],
    }]


def _skill_entries(selected: dict[str, dict[str, Any]]) -> dict[str, str]:
    entries: dict[str, str] = {}
    owners: dict[str, str] = {}
    for extension_id, item in selected.items():
        record = item["record"]
        instance = item["instance"]
        requested = set(instance.get("skills") or [])
        if not requested:
            continue
        install_root = extension_store.runtime_package_root_for_record(record)
        if install_root is None:
            raise HarnessProfileResolutionError(f"Extension package is unavailable: {extension_id}")
        root = install_root.resolve()
        for skill in (record.get("manifest") or {}).get("entrypoints", {}).get("skills") or []:
            if not isinstance(skill, dict) or skill.get("name") not in requested:
                continue
            skill_name = str(skill["name"])
            owner = owners.get(skill_name)
            if owner and owner != extension_id:
                raise HarnessProfileResolutionError(f"Duplicate selected skill name: {skill_name}")
            skill_dir = (root / str(skill.get("path") or "")).resolve()
            skill_md = skill_dir / "SKILL.md"
            if not skill_dir.is_relative_to(root) or not skill_md.is_file():
                raise HarnessProfileResolutionError(f"Skill path is unavailable: {extension_id}.{skill_name}")
            entries[skill_name] = skill_md.read_text(encoding="utf-8")
            owners[skill_name] = extension_id
    return entries


def _normalize_capability_contexts(value: Any) -> list[dict[str, Any]]:
    try:
        return capability_contexts_mod.normalize_capability_contexts(value)
    except ValueError as exc:
        raise HarnessProfileResolutionError(str(exc)) from exc


def resolve_for_session(
    session: dict[str, Any] | None,
    *,
    profile_id: str | None = None,
    revision: str | None = None,
    turn_capability_contexts: list[dict] | None = None,
) -> dict[str, Any] | None:
    session = session or {}
    selected_id = str(profile_id or session.get("harness_profile_id") or "").strip()
    if not selected_id:
        return None
    selected_revision = str(revision or session.get("harness_profile_revision") or "").strip()
    resolved = resolve_profile(selected_id, selected_revision or None)

    selected: dict[str, dict[str, Any]] = {}
    extension_mcp_servers: dict[str, list[str]] = {}
    extension_skills: dict[str, list[str]] = {}
    extension_instruction_names: dict[str, list[str]] = {}
    extension_revisions: dict[str, str] = {}
    extension_setting_overlays: dict[str, dict[str, Any]] = {}
    secret_refs: dict[str, list[str]] = {}
    for extension_id, fields in resolved["extension_instances"].items():
        mcp_servers = fields["mcp_servers"]["resolved"]
        skills = fields["skills"]["resolved"]
        instruction_names = fields["instruction_names"]["resolved"]
        overlay_fields = fields.get("setting_overlays") or {}
        if not (mcp_servers or skills or instruction_names or overlay_fields):
            continue
        record = _runtime_ready_record(extension_id)
        actual_revision = _extension_revision(record)
        # The drift check is scoped ONLY to instances that carry an explicit
        # pinned extension_revision override; inherited (unpinned) instances
        # never raise here.
        expected_revision = fields.get("extension_revision", {}).get("override")
        if expected_revision and actual_revision and str(expected_revision) != actual_revision:
            raise HarnessProfileResolutionError(f"Extension revision changed: {extension_id}")
        items = _extension_items(record)
        for server_name in mcp_servers:
            if server_name not in items["mcp"]:
                raise HarnessProfileResolutionError(f"Unknown extension MCP server: {extension_id}.{server_name}")
        for skill_name in skills:
            if skill_name not in items["skills"]:
                raise HarnessProfileResolutionError(f"Unknown extension skill: {extension_id}.{skill_name}")
        for instruction_name in instruction_names:
            if instruction_name not in items["instructions"]:
                raise HarnessProfileResolutionError(f"Unknown extension instruction: {extension_id}.{instruction_name}")
        selected[extension_id] = {"record": record, "instance": {"skills": skills}}
        extension_revisions[extension_id] = actual_revision
        extension_mcp_servers[extension_id] = list(mcp_servers)
        extension_skills[extension_id] = list(skills)
        extension_instruction_names[extension_id] = list(instruction_names)
        settings: dict[str, Any] = {}
        for key, entry in overlay_fields.items():
            item = entry["resolved"]
            expected = str((item or {}).get("schema_hash") or "")
            actual = _setting_schema_hash(record, key)
            if expected and expected != actual:
                raise HarnessProfileResolutionError(f"Extension setting schema changed: {extension_id}.{key}")
            settings[key] = item
        if settings:
            extension_setting_overlays[extension_id] = settings
        live_secret_refs = fields.get("secret_refs", {}).get("resolved") or []
        if live_secret_refs:
            secret_refs[extension_id] = list(live_secret_refs)
    secret_refs.update(copy.deepcopy(resolved.get("secret_refs") or {}))
    instruction_sources = {name: entry["resolved"] for name, entry in resolved["instruction_sources"].items()}
    instruction_blocks = _instruction_blocks(instruction_sources, selected)
    profile_contexts = _provider_context_blocks(instruction_blocks)
    profile_contexts.extend(_normalize_capability_contexts(resolved.get("capability_contexts")))
    if turn_capability_contexts:
        profile_contexts.extend(turn_capability_contexts)
    provider_run_config = copy.deepcopy(resolved.get("provider_run_config_overlay") or {})
    skill_entries = _skill_entries(selected)
    if skill_entries:
        provider_run_config["skills"] = {
            **copy.deepcopy(provider_run_config.get("skills") or {}),
            **skill_entries,
        }
    snapshot = {
        "profile_id": resolved["id"],
        "profile_revision": resolved.get("revision") or "",
        "profile_name": resolved.get("name") or resolved["id"],
        # base_mode was removed from the profile schema in v2 (section 1);
        # "bare" is now purely a session-level concern.
        "bare_config": bool(session.get("bare_config")),
        "capability_contexts": profile_contexts,
        "provider_run_config": provider_run_config,
        "extra_mcp_servers": sorted({server for servers in extension_mcp_servers.values() for server in servers}),
        "active_capability_ids": [
            str(item)
            for item in session.get("active_capability_ids") or []
            if str(item or "").strip()
        ],
        "disabled_builtin_tools": list(resolved["disabled_builtin_tools"]["resolved"]),
        "disabled_builtin_extensions": list(resolved["disabled_builtin_extensions"]["resolved"]),
        "extension_revisions": extension_revisions,
        "extension_mcp_servers": extension_mcp_servers,
        "extension_skills": extension_skills,
        "extension_instruction_names": extension_instruction_names,
        "extension_setting_overlays": extension_setting_overlays,
        "secret_refs": secret_refs,
        "instruction_blocks": instruction_blocks,
    }
    snapshot["launcher_projection"] = {
        "profile_id": snapshot["profile_id"],
        "profile_revision": snapshot["profile_revision"],
        "bare_config": snapshot["bare_config"],
        "extension_revisions": extension_revisions,
        "extension_mcp_servers": extension_mcp_servers,
        "extension_setting_overlays": snapshot["extension_setting_overlays"],
        "secret_refs": snapshot["secret_refs"],
        "disabled_builtin_extensions": snapshot["disabled_builtin_extensions"],
        "disabled_builtin_tools": snapshot["disabled_builtin_tools"],
        "active_capability_ids": snapshot["active_capability_ids"],
    }
    return snapshot
