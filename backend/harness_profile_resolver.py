from __future__ import annotations

import copy
from typing import Any

import capability_contexts as capability_contexts_mod
import extension_instructions
import extension_store
import harness_profile_store


class HarnessProfileResolutionError(ValueError):
    pass


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


def _instruction_blocks(profile: dict[str, Any], selected: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in profile.get("instruction_sources") or []:
        if source.get("kind") == "inline":
            key = ("inline", source["name"])
            if key in seen:
                continue
            seen.add(key)
            blocks.append({
                "source": "profile",
                "name": source["name"],
                "content": source["content"],
            })
            continue
        extension_id = source.get("extension_id")
        if extension_id not in selected:
            raise HarnessProfileResolutionError(f"Instruction source extension is not selected: {extension_id}")
        record = selected[extension_id]["record"]
        install_root = extension_store.runtime_package_root_for_record(record)
        if install_root is None:
            raise HarnessProfileResolutionError(f"Extension package is unavailable: {extension_id}")
        for item in extension_instructions.instruction_items_from_entrypoints((record.get("manifest") or {}).get("entrypoints") or {}) or []:
            if not isinstance(item, dict) or item.get("name") != source.get("name"):
                continue
            content_path = (install_root / item["path"]).resolve()
            root = install_root.resolve()
            if not content_path.is_relative_to(root) or not content_path.is_file():
                raise HarnessProfileResolutionError(f"Instruction path is unavailable: {extension_id}.{source.get('name')}")
            key = (extension_id, str(source.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            blocks.append({
                "source": extension_id,
                "name": str(source.get("name") or ""),
                "content": content_path.read_text(encoding="utf-8").strip(),
            })
            break
        else:
            raise HarnessProfileResolutionError(f"Unknown extension instruction: {extension_id}.{source.get('name')}")
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
    profile = harness_profile_store.get_profile(selected_id, selected_revision or None)
    if not profile:
        raise HarnessProfileResolutionError("Harness profile is missing or its pinned revision is unavailable")
    selected: dict[str, dict[str, Any]] = {}
    extension_mcp_servers: dict[str, list[str]] = {}
    extension_skills: dict[str, list[str]] = {}
    extension_instruction_names: dict[str, list[str]] = {}
    extension_revisions: dict[str, str] = {}
    for instance in profile.get("extension_instances") or []:
        extension_id = instance["extension_id"]
        record = _runtime_ready_record(extension_id)
        actual_revision = _extension_revision(record)
        expected_revision = str(instance.get("extension_revision") or "").strip()
        if expected_revision and actual_revision and expected_revision != actual_revision:
            raise HarnessProfileResolutionError(f"Extension revision changed: {extension_id}")
        items = _extension_items(record)
        for server_name in instance.get("mcp_servers") or []:
            if server_name not in items["mcp"]:
                raise HarnessProfileResolutionError(f"Unknown extension MCP server: {extension_id}.{server_name}")
        for skill_name in instance.get("skills") or []:
            if skill_name not in items["skills"]:
                raise HarnessProfileResolutionError(f"Unknown extension skill: {extension_id}.{skill_name}")
        for instruction_name in instance.get("instruction_names") or []:
            if instruction_name not in items["instructions"]:
                raise HarnessProfileResolutionError(f"Unknown extension instruction: {extension_id}.{instruction_name}")
        selected[extension_id] = {"record": record, "instance": instance}
        extension_revisions[extension_id] = actual_revision
        extension_mcp_servers[extension_id] = list(instance.get("mcp_servers") or [])
        extension_skills[extension_id] = list(instance.get("skills") or [])
        extension_instruction_names[extension_id] = list(instance.get("instruction_names") or [])
    for extension_id, settings in (profile.get("extension_setting_overlays") or {}).items():
        record = selected.get(extension_id, {}).get("record") or _runtime_ready_record(extension_id)
        for key, item in settings.items():
            expected = str((item or {}).get("schema_hash") or "")
            actual = _setting_schema_hash(record, key)
            if expected and expected != actual:
                raise HarnessProfileResolutionError(f"Extension setting schema changed: {extension_id}.{key}")
    instruction_blocks = _instruction_blocks(profile, selected)
    profile_contexts = _provider_context_blocks(instruction_blocks)
    profile_contexts.extend(_normalize_capability_contexts(profile.get("capability_contexts")))
    if turn_capability_contexts:
        profile_contexts.extend(turn_capability_contexts)
    provider_run_config = copy.deepcopy(profile.get("provider_run_config_overlay") or {})
    skill_entries = _skill_entries(selected)
    if skill_entries:
        provider_run_config["skills"] = {
            **copy.deepcopy(provider_run_config.get("skills") or {}),
            **skill_entries,
        }
    snapshot = {
        "profile_id": profile["id"],
        "profile_revision": profile["revision"],
        "profile_name": profile.get("name") or profile["id"],
        "bare_config": profile.get("base_mode") == "bare",
        "capability_contexts": profile_contexts,
        "provider_run_config": provider_run_config,
        "extra_mcp_servers": sorted({server for servers in extension_mcp_servers.values() for server in servers}),
        "active_capability_ids": [
            str(item)
            for item in session.get("active_capability_ids") or []
            if str(item or "").strip()
        ],
        "disabled_builtin_tools": list(profile.get("disabled_builtin_tools") or []),
        "disabled_builtin_extensions": list(profile.get("disabled_builtin_extensions") or []),
        "extension_revisions": extension_revisions,
        "extension_mcp_servers": extension_mcp_servers,
        "extension_skills": extension_skills,
        "extension_instruction_names": extension_instruction_names,
        "extension_setting_overlays": copy.deepcopy(profile.get("extension_setting_overlays") or {}),
        "secret_refs": copy.deepcopy(profile.get("secret_refs") or {}),
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
