from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from datetime import datetime
from typing import Any

from json_store import read_json, write_json
from paths import ba_home


# v1 was an independent full-snapshot record per profile. v2 replaces that
# with a Default-synthesis + sparse-override inheritance model: a stored
# profile only carries the deltas it explicitly overrides; everything else
# inherits live Default state at resolve time (see harness_profile_resolver).
SCHEMA_VERSION = 2
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 1_000
MAX_INLINE_INSTRUCTION_CHARS = 80_000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_LOCK = threading.RLock()
DEFAULT_PROFILE_ID = "default"


class HarnessProfileError(ValueError):
    pass


def _path():
    return ba_home() / "harness_profiles.json"


def _blank() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profiles": {}}


def _now() -> str:
    return datetime.now().isoformat()


def _load() -> dict[str, Any]:
    data = read_json(_path(), _blank())
    if data.get("schema_version") != SCHEMA_VERSION:
        raise HarnessProfileError("Harness profiles are incompatible with this Better Agent version")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise HarnessProfileError("Malformed harness profiles: profiles must be an object")
    return data


def _save(data: dict[str, Any]) -> None:
    write_json(_path(), data)


def _clean_id(value: object, *, required: bool = True) -> str:
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value.strip()):
        raise HarnessProfileError("Harness profile id must be 3-80 lowercase letters, numbers, dots, dashes, or underscores")
    return value.strip()


def _clean_text(value: object, field: str, max_chars: int, *, required: bool = False) -> str:
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str):
        raise HarnessProfileError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise HarnessProfileError(f"{field} is required")
    if len(text) > max_chars:
        raise HarnessProfileError(f"{field} is too large")
    return text


def _string_list(value: object, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise HarnessProfileError(f"{field} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HarnessProfileError(f"{field} entries must be non-empty strings")
        clean = item.strip()
        if clean not in out:
            out.append(clean)
    return out


def _string_map(value: object, field: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise HarnessProfileError(f"{field} must be an object")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise HarnessProfileError(f"{field} keys must be non-empty strings")
        out[key.strip()] = copy.deepcopy(item)
    return out


def _normalize_secret_refs(value: object) -> dict[str, list[str]]:
    """secret_refs is dict[extension_id, list[str]] — opaque token
    references, never arbitrary data."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise HarnessProfileError("secret_refs must be an object")
    out: dict[str, list[str]] = {}
    for extension_id, refs in value.items():
        clean_id = _clean_id(extension_id)
        out[clean_id] = _string_list(refs, f"secret_refs.{clean_id}")
    return out


def _normalize_delta(value: object, field: str) -> dict[str, list[str]] | None:
    """Validate a sparse `{"add": [...], "remove": [...]}` delta. `None`
    means "no override for this leaf" (inherit Default)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HarnessProfileError(f"{field} must be an object with add/remove lists")
    add = _string_list(value.get("add"), f"{field}.add")
    remove = _string_list(value.get("remove"), f"{field}.remove")
    return {"add": add, "remove": remove}


def _normalize_setting_overlay_entry(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or "value" not in value:
        raise HarnessProfileError(f"{field} must be an object with a value")
    return {
        "value": copy.deepcopy(value["value"]),
        "schema_hash": _clean_text(value.get("schema_hash"), f"{field}.schema_hash", 128),
    }


def _normalize_extension_instance_override(value: object, extension_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessProfileError(f"extension_instances.{extension_id} must be an object")
    out: dict[str, Any] = {}
    if "extension_revision" in value:
        out["extension_revision"] = _clean_text(
            value.get("extension_revision"),
            f"extension_instances.{extension_id}.extension_revision",
            200,
        ) or None
    if "mcp_servers" in value:
        out["mcp_servers"] = _normalize_delta(
            value.get("mcp_servers"), f"extension_instances.{extension_id}.mcp_servers"
        )
    if "skills" in value:
        out["skills"] = _normalize_delta(
            value.get("skills"), f"extension_instances.{extension_id}.skills"
        )
    if "instruction_names" in value:
        out["instruction_names"] = _normalize_delta(
            value.get("instruction_names"), f"extension_instances.{extension_id}.instruction_names"
        )
    if "setting_overlays" in value:
        raw = value.get("setting_overlays")
        if raw is None:
            out["setting_overlays"] = None
        else:
            if not isinstance(raw, dict):
                raise HarnessProfileError(f"extension_instances.{extension_id}.setting_overlays must be an object")
            out["setting_overlays"] = {
                str(key): _normalize_setting_overlay_entry(
                    item, f"extension_instances.{extension_id}.setting_overlays.{key}"
                )
                for key, item in raw.items()
            }
    if "headless" in value:
        headless = value.get("headless")
        if headless is not None and not isinstance(headless, bool):
            raise HarnessProfileError(f"extension_instances.{extension_id}.headless must be a boolean")
        out["headless"] = headless
    return out


def _normalize_instruction_source_override(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessProfileError(f"instruction_sources.{name} must be an object")
    kind = _clean_text(value.get("kind"), f"instruction_sources.{name}.kind", 40, required=True)
    if kind not in ("inline", "extension"):
        raise HarnessProfileError("instruction source kind must be inline or extension")
    entry: dict[str, Any] = {"kind": kind}
    if kind == "inline":
        entry["content"] = _clean_text(
            value.get("content"), f"instruction_sources.{name}.content", MAX_INLINE_INSTRUCTION_CHARS, required=True
        )
    else:
        entry["extension_id"] = _clean_id(value.get("extension_id"))
    return entry


def _normalize_overrides(value: object) -> dict[str, Any]:
    if value in (None, ""):
        value = {}
    if not isinstance(value, dict):
        raise HarnessProfileError("overrides must be an object")
    out: dict[str, Any] = {}
    raw_instances = value.get("extension_instances")
    if raw_instances:
        if not isinstance(raw_instances, dict):
            raise HarnessProfileError("overrides.extension_instances must be an object")
        instances: dict[str, Any] = {}
        for extension_id, item in raw_instances.items():
            clean_id = _clean_id(extension_id)
            instances[clean_id] = _normalize_extension_instance_override(item, clean_id)
        if instances:
            out["extension_instances"] = instances
    for field in ("disabled_builtin_tools", "disabled_builtin_extensions"):
        delta = _normalize_delta(value.get(field), f"overrides.{field}")
        if delta is not None:
            out[field] = delta
    raw_sources = value.get("instruction_sources")
    if raw_sources:
        if not isinstance(raw_sources, dict):
            raise HarnessProfileError("overrides.instruction_sources must be an object")
        sources: dict[str, Any] = {}
        for name, item in raw_sources.items():
            if not isinstance(name, str) or not name.strip():
                raise HarnessProfileError("instruction_sources keys must be non-empty strings")
            # Absence of a key already means "inherit"; only explicit
            # overrides (inline/extension source dicts) are stored here.
            sources[name.strip()] = _normalize_instruction_source_override(item, name.strip())
        if sources:
            out["instruction_sources"] = sources
    return out


def _normalized_payload(profile_id: str, payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HarnessProfileError("Harness profile payload must be an object")
    now = _now()
    profile = {
        "id": profile_id,
        "schema_version": SCHEMA_VERSION,
        "name": _clean_text(payload.get("name"), "name", MAX_NAME_CHARS, required=True),
        "description": _clean_text(payload.get("description"), "description", MAX_DESCRIPTION_CHARS),
        "source": _clean_text(payload.get("source"), "source", 80),
        "overrides": _normalize_overrides(payload.get("overrides", {})),
        "mcp_overrides": _string_map(payload.get("mcp_overrides"), "mcp_overrides"),
        "skill_overrides": _string_map(payload.get("skill_overrides"), "skill_overrides"),
        "native_harness_overrides": _string_map(payload.get("native_harness_overrides"), "native_harness_overrides"),
        "provider_run_config_overlay": _string_map(payload.get("provider_run_config_overlay"), "provider_run_config_overlay"),
        "capability_contexts": copy.deepcopy(payload.get("capability_contexts") or []),
        "secret_refs": _normalize_secret_refs(payload.get("secret_refs")),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    profile["revision"] = _revision(profile)
    return profile


def _revision(profile: dict[str, Any]) -> str:
    data = copy.deepcopy(profile)
    data.pop("revision", None)
    data.pop("updated_at", None)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:16]


def list_profiles() -> list[dict[str, Any]]:
    with _LOCK:
        profiles = list(_load()["profiles"].values())
    return sorted((copy.deepcopy(p) for p in profiles), key=lambda p: (p.get("name") or "", p.get("id") or ""))


def get_profile(profile_id: str, revision: str | None = None) -> dict[str, Any] | None:
    clean_id = _clean_id(profile_id)
    if clean_id == DEFAULT_PROFILE_ID:
        raise HarnessProfileError("the default profile is not a stored profile")
    with _LOCK:
        profile = _load()["profiles"].get(clean_id)
    if not profile:
        return None
    if revision and profile.get("revision") != revision:
        return None
    return copy.deepcopy(profile)


def upsert_profile(payload: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    """Shared low-level normalize-and-write primitive. `create_profile` and
    `apply_override_patch` both build on this; callers are responsible for
    shaping `payload` (e.g. forcing `overrides={}` on create)."""
    with _LOCK:
        data = _load()
        clean_id = _clean_id(profile_id or payload.get("id"))
        if clean_id == DEFAULT_PROFILE_ID:
            raise HarnessProfileError("the default profile is not a stored profile")
        existing = data["profiles"].get(clean_id)
        profile = _normalized_payload(clean_id, payload, existing=existing)
        data["profiles"][clean_id] = profile
        _save(data)
        return copy.deepcopy(profile)


def create_profile(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HarnessProfileError("Harness profile payload must be an object")
    profile_id = _clean_id(payload.get("id") or None, required=False)
    if not profile_id:
        raise HarnessProfileError("id is required")
    clean_payload = {
        "id": profile_id,
        "name": payload.get("name"),
        "description": payload.get("description"),
        "source": payload.get("source"),
        "overrides": {},
    }
    return upsert_profile(clean_payload, profile_id)


def _get_at_path(tree: dict[str, Any], path: list[str]) -> Any:
    node: Any = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set_at_path(tree: dict[str, Any], path: list[str], value: Any) -> None:
    node = tree
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _clear_at_path(tree: dict[str, Any], path: list[str]) -> None:
    nodes: list[dict[str, Any]] = [tree]
    node: Any = tree
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
        nodes.append(node)
    if not isinstance(node, dict):
        return
    node.pop(path[-1], None)
    # Prune now-empty intermediate dicts so a cleared leaf doesn't leave
    # a stray empty override container behind.
    for parent, key in zip(reversed(nodes[:-1]), reversed(path[:-1])):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
        else:
            break


_KNOWN_OVERRIDE_TOP_LEVEL_KEYS = (
    "extension_instances", "disabled_builtin_tools", "disabled_builtin_extensions", "instruction_sources",
)


def _validate_override_path(path: list[str]) -> None:
    if not isinstance(path, list) or not path or not all(isinstance(p, str) and p for p in path):
        raise HarnessProfileError("op path must be a non-empty list of strings")
    if path[0] not in _KNOWN_OVERRIDE_TOP_LEVEL_KEYS:
        raise HarnessProfileError(f"op path must start with one of {_KNOWN_OVERRIDE_TOP_LEVEL_KEYS}")


def apply_override_patch(profile_id: str, ops: list[dict[str, Any]], revision: str | None = None) -> dict[str, Any]:
    if not isinstance(ops, list) or not ops:
        raise HarnessProfileError("ops must be a non-empty list")
    with _LOCK:
        data = _load()
        clean_id = _clean_id(profile_id)
        if clean_id == DEFAULT_PROFILE_ID:
            raise HarnessProfileError("the default profile is not a stored profile")
        existing = data["profiles"].get(clean_id)
        if not existing:
            raise HarnessProfileError("harness profile not found")
        if revision and existing.get("revision") != revision:
            raise HarnessProfileError("Harness profile changed; reload before editing")
        overrides = copy.deepcopy(existing.get("overrides") or {})
        for op in ops:
            if not isinstance(op, dict):
                raise HarnessProfileError("each op must be an object")
            path = op.get("path")
            _validate_override_path(path)
            kind = op.get("op")
            if kind == "clear":
                _clear_at_path(overrides, path)
            elif kind == "set":
                _set_at_path(overrides, path, copy.deepcopy(op.get("value")))
            else:
                raise HarnessProfileError("op.op must be set or clear")
        payload = {
            "id": clean_id,
            "name": existing.get("name"),
            "description": existing.get("description"),
            "source": existing.get("source"),
            "overrides": overrides,
            "mcp_overrides": existing.get("mcp_overrides"),
            "skill_overrides": existing.get("skill_overrides"),
            "native_harness_overrides": existing.get("native_harness_overrides"),
            "provider_run_config_overlay": existing.get("provider_run_config_overlay"),
            "capability_contexts": existing.get("capability_contexts"),
            "secret_refs": existing.get("secret_refs"),
        }
        profile = _normalized_payload(clean_id, payload, existing=existing)
        data["profiles"][clean_id] = profile
        _save(data)
        return copy.deepcopy(profile)


def delete_profile(profile_id: str, revision: str | None = None) -> bool:
    clean_id = _clean_id(profile_id)
    if clean_id == DEFAULT_PROFILE_ID:
        raise HarnessProfileError("the default profile cannot be deleted")
    with _LOCK:
        data = _load()
        profile = data["profiles"].get(clean_id)
        if not profile:
            return False
        if revision and profile.get("revision") != revision:
            raise HarnessProfileError("Harness profile changed; reload before deleting")
        data["profiles"].pop(clean_id, None)
        _save(data)
        return True
