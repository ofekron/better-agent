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


SCHEMA_VERSION = 1
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 1_000
MAX_INLINE_INSTRUCTION_CHARS = 80_000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_LOCK = threading.RLock()


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


def _normalize_surfaces(value: object, field: str) -> list[str]:
    allowed = {"instructions", "skills", "mcp", "applied_config", "frontend"}
    surfaces = _string_list(value, field)
    unknown = [item for item in surfaces if item not in allowed]
    if unknown:
        raise HarnessProfileError(f"{field} contains unsupported surfaces: {', '.join(unknown)}")
    return surfaces


def _normalize_extension_instances(value: object) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise HarnessProfileError("extension_instances must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise HarnessProfileError(f"extension_instances[{index}] must be an object")
        extension_id = _clean_id(item.get("extension_id"))
        if extension_id in seen:
            raise HarnessProfileError(f"duplicate extension_instances entry: {extension_id}")
        seen.add(extension_id)
        out.append({
            "extension_id": extension_id,
            "extension_revision": _clean_text(item.get("extension_revision"), f"extension_instances[{index}].extension_revision", 200),
            "surfaces": _normalize_surfaces(item.get("surfaces") or [], f"extension_instances[{index}].surfaces"),
            "mcp_servers": _string_list(item.get("mcp_servers"), f"extension_instances[{index}].mcp_servers"),
            "skills": _string_list(item.get("skills"), f"extension_instances[{index}].skills"),
            "instruction_names": _string_list(item.get("instruction_names"), f"extension_instances[{index}].instruction_names"),
        })
    return out


def _normalize_setting_overlays(value: object) -> dict[str, dict[str, Any]]:
    raw = _string_map(value, "extension_setting_overlays")
    out: dict[str, dict[str, Any]] = {}
    for extension_id, settings in raw.items():
        _clean_id(extension_id)
        if not isinstance(settings, dict):
            raise HarnessProfileError("extension_setting_overlays values must be objects")
        clean_settings: dict[str, Any] = {}
        for key, item in settings.items():
            if not isinstance(key, str) or not key.strip():
                raise HarnessProfileError("extension setting keys must be non-empty strings")
            if isinstance(item, dict):
                if "value" not in item:
                    raise HarnessProfileError("setting overlay objects must include value")
                clean_settings[key.strip()] = {
                    "value": copy.deepcopy(item["value"]),
                    "schema_hash": _clean_text(item.get("schema_hash"), "setting schema_hash", 128),
                }
            else:
                clean_settings[key.strip()] = {"value": copy.deepcopy(item), "schema_hash": ""}
        out[extension_id] = clean_settings
    return out


def _normalize_secret_refs(value: object) -> dict[str, list[str]]:
    raw = _string_map(value, "secret_refs")
    out: dict[str, list[str]] = {}
    for extension_id, refs in raw.items():
        _clean_id(extension_id)
        out[extension_id] = _string_list(refs, f"secret_refs.{extension_id}")
    return out


def _normalize_instruction_sources(value: object) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise HarnessProfileError("instruction_sources must be a list")
    out: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise HarnessProfileError(f"instruction_sources[{index}] must be an object")
        kind = _clean_text(item.get("kind"), f"instruction_sources[{index}].kind", 40, required=True)
        if kind not in ("inline", "extension"):
            raise HarnessProfileError("instruction source kind must be inline or extension")
        entry = {"kind": kind}
        if kind == "inline":
            entry["name"] = _clean_text(item.get("name"), "instruction name", 120, required=True)
            entry["content"] = _clean_text(item.get("content"), "instruction content", MAX_INLINE_INSTRUCTION_CHARS, required=True)
        else:
            entry["extension_id"] = _clean_id(item.get("extension_id"))
            entry["name"] = _clean_text(item.get("name"), "instruction name", 120, required=True)
        out.append(entry)
    return out


def _normalized_payload(profile_id: str, payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HarnessProfileError("Harness profile payload must be an object")
    base_mode = str(payload.get("base_mode") or "inherit").strip()
    if base_mode not in ("inherit", "bare"):
        raise HarnessProfileError("base_mode must be inherit or bare")
    now = _now()
    profile = {
        "id": profile_id,
        "schema_version": SCHEMA_VERSION,
        "name": _clean_text(payload.get("name"), "name", MAX_NAME_CHARS, required=True),
        "description": _clean_text(payload.get("description"), "description", MAX_DESCRIPTION_CHARS),
        "base_mode": base_mode,
        "extension_instances": _normalize_extension_instances(payload.get("extension_instances")),
        "extension_setting_overlays": _normalize_setting_overlays(payload.get("extension_setting_overlays")),
        "secret_refs": _normalize_secret_refs(payload.get("secret_refs")),
        "mcp_overrides": _string_map(payload.get("mcp_overrides"), "mcp_overrides"),
        "skill_overrides": _string_map(payload.get("skill_overrides"), "skill_overrides"),
        "native_harness_overrides": _string_map(payload.get("native_harness_overrides"), "native_harness_overrides"),
        "instruction_sources": _normalize_instruction_sources(payload.get("instruction_sources")),
        "provider_run_config_overlay": _string_map(payload.get("provider_run_config_overlay"), "provider_run_config_overlay"),
        "capability_contexts": copy.deepcopy(payload.get("capability_contexts") or []),
        "disabled_builtin_tools": _string_list(payload.get("disabled_builtin_tools"), "disabled_builtin_tools"),
        "disabled_builtin_extensions": _string_list(payload.get("disabled_builtin_extensions"), "disabled_builtin_extensions"),
        "source": _clean_text(payload.get("source"), "source", 80),
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
    with _LOCK:
        profile = _load()["profiles"].get(clean_id)
    if not profile:
        return None
    if revision and profile.get("revision") != revision:
        return None
    return copy.deepcopy(profile)


def upsert_profile(payload: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    with _LOCK:
        data = _load()
        clean_id = _clean_id(profile_id or payload.get("id"))
        existing = data["profiles"].get(clean_id)
        profile = _normalized_payload(clean_id, payload, existing=existing)
        data["profiles"][clean_id] = profile
        _save(data)
        return copy.deepcopy(profile)


def delete_profile(profile_id: str, revision: str | None = None) -> bool:
    clean_id = _clean_id(profile_id)
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
