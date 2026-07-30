from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any

from harness_secret_refs import (
    HarnessSecretRefError,
    normalize_harness_secret_refs,
)
from paths import ba_home


# v1 was an independent full-snapshot record per profile. v2 replaces that
# with a Default-synthesis + sparse-override inheritance model: a stored
# profile only carries the deltas it explicitly overrides; everything else
# inherits live Default state at resolve time (see harness_profile_resolver).
# v3 adds optional profile-to-profile inheritance (base_profile_id) and
# optional provider/model/reasoning-effort pins consulted as a
# session-creation fallback.
# v4 adds disabled_runtime_skills so harness profiles cover the full
# capability-narrowing surface. v5 adds an optional provisioning_prompt used
# as the default worker-provisioning prompt. v6 re-expresses the provider pin
# as a runtime-profile pin (default_runtime_profile_id).
# This store migrates: contiguous N->N+1 migrations upgrade any version in
# [MIN_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION) at connect time; only stores
# below the minimum (or from a newer Better Agent) are refused.
SCHEMA_VERSION = 6
MIN_SUPPORTED_SCHEMA_VERSION = 5
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 1_000
MAX_INLINE_INSTRUCTION_CHARS = 80_000
MAX_SELECTOR_CHARS = 200
MAX_PROVISIONING_PROMPT_CHARS = 80_000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_LOCK = threading.RLock()
DEFAULT_PROFILE_ID = "default"


class HarnessProfileError(ValueError):
    pass


class HarnessProfileNotFoundError(HarnessProfileError):
    """The addressed profile is not in the store. Separate from a validation
    failure so the API layer can answer 404 instead of 400 — the editor keys
    its "selection was deleted" recovery on that status."""


# Document-per-row SQLite substrate (ADR-0001): one row per profile id,
# validated/normalized JSON in `data`, WAL mode so readers never block behind
# a writer. `meta.store_version` is a monotonic counter bumped inside the
# same transaction as every write, giving `store_fingerprint()` an O(1)
# change-detection signal instead of stat()-ing a whole-file mtime/size.
_DB_LOCK = threading.RLock()


def _db_path():
    return ba_home() / "harness_profiles.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profiles ("
        "id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, "
        "revision TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('store_version', '0')"
            )
    else:
        stored_version = int(row[0])
        if (
            stored_version < MIN_SUPPORTED_SCHEMA_VERSION
            or stored_version > SCHEMA_VERSION
        ):
            conn.close()
            raise HarnessProfileError(
                "Harness profiles are incompatible with this Better Agent version"
            )
        if stored_version < SCHEMA_VERSION:
            try:
                _migrate_store(conn, stored_version)
            except Exception:
                conn.close()
                raise
    return conn


def _migrate_5_to_6(profile: dict) -> dict:
    """v6: the provider pin becomes a runtime-profile pin. The pinned
    provider maps to its preferred live profile; a provider with no live
    profile (or gone entirely) drops the pin."""
    import config_store

    migrated = dict(profile)
    pinned_provider = migrated.pop("default_provider_id", None)
    pinned_profile_id = None
    if pinned_provider:
        pinned_profile_id = config_store.provider_execution_defaults(
            str(pinned_provider)
        )["runtime_profile_id"]
    migrated["default_runtime_profile_id"] = pinned_profile_id
    migrated["schema_version"] = 6
    return migrated


_SCHEMA_MIGRATIONS: dict[int, Any] = {
    5: _migrate_5_to_6,
}


def _validate_schema_migrations() -> None:
    expected = set(range(MIN_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION))
    if set(_SCHEMA_MIGRATIONS) != expected:
        raise HarnessProfileError(
            "harness profile schema migration chain is incomplete"
        )


def _migrate_store(conn: sqlite3.Connection, stored_version: int) -> None:
    """Upgrade every profile row through the contiguous migration chain and
    stamp the new store schema version in the same transaction."""
    _validate_schema_migrations()
    with conn:
        rows = conn.execute("SELECT id, data FROM profiles").fetchall()
        version = stored_version
        while version < SCHEMA_VERSION:
            step = _SCHEMA_MIGRATIONS[version]
            migrated_rows = []
            for profile_id, data in rows:
                profile = json.loads(data)
                profile = step(profile)
                migrated_rows.append((profile_id, json.dumps(profile)))
            rows = migrated_rows
            version += 1
        for profile_id, data in rows:
            conn.execute(
                "UPDATE profiles SET data = ?, schema_version = ? WHERE id = ?",
                (data, SCHEMA_VERSION, profile_id),
            )
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        _bump_version(conn)


def _bump_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('store_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def store_fingerprint() -> tuple[int, int]:
    if not _db_path().exists():
        return (0, 0)
    with _DB_LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'store_version'").fetchone()
        finally:
            conn.close()
    return (int(row[0]) if row else 0, 0)


def _blank() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profiles": {}}


def _now() -> str:
    return datetime.now().isoformat()


def _load() -> dict[str, Any]:
    with _DB_LOCK:
        conn = _connect()
        try:
            rows = conn.execute("SELECT id, data FROM profiles").fetchall()
        finally:
            conn.close()
    profiles = {row[0]: json.loads(row[1]) for row in rows}
    return {"schema_version": SCHEMA_VERSION, "profiles": profiles}


def _upsert_profile_row(conn: sqlite3.Connection, profile: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO profiles(id, schema_version, revision, updated_at, data) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET schema_version=excluded.schema_version, "
        "revision=excluded.revision, updated_at=excluded.updated_at, data=excluded.data",
        (
            profile["id"],
            profile.get("schema_version", SCHEMA_VERSION),
            profile["revision"],
            profile["updated_at"],
            json.dumps(profile, sort_keys=True, separators=(",", ":")),
        ),
    )


def _write_profile_row(profile: dict[str, Any]) -> None:
    """Row-level write choke point: touches exactly the one changed record,
    not the whole store — the O(1)-write property ADR-0001 is for."""
    with _DB_LOCK:
        conn = _connect()
        try:
            with conn:
                _upsert_profile_row(conn, profile)
                _bump_version(conn)
        finally:
            conn.close()


def _delete_profile_row(profile_id: str) -> None:
    with _DB_LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
                _bump_version(conn)
        finally:
            conn.close()


def _replace_all_profiles(profiles: dict[str, Any]) -> None:
    """Full-store replace for `import_harness_sync_state`: the operation's
    contract is "replace this host's store", so an O(n) rewrite here is
    inherent to the operation, not a regression from the JSON-file era."""
    with _DB_LOCK:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM profiles")
                for profile in profiles.values():
                    _upsert_profile_row(conn, profile)
                _bump_version(conn)
        finally:
            conn.close()


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
    try:
        return normalize_harness_secret_refs(value)
    except HarnessSecretRefError as exc:
        raise HarnessProfileError(str(exc)) from exc


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
    for field in ("disabled_builtin_tools", "disabled_builtin_extensions", "disabled_runtime_skills"):
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
        "secret_refs": _normalize_secret_refs(
            payload.get("secret_refs", {}),
        ),
        "base_profile_id": _clean_id(payload.get("base_profile_id"), required=False) or None,
        "base_profile_revision": _clean_text(payload.get("base_profile_revision"), "base_profile_revision", 64) or None,
        "default_runtime_profile_id": _clean_text(payload.get("default_runtime_profile_id"), "default_runtime_profile_id", MAX_SELECTOR_CHARS) or None,
        "default_model": _clean_text(payload.get("default_model"), "default_model", MAX_SELECTOR_CHARS) or None,
        "default_reasoning_effort": _clean_text(payload.get("default_reasoning_effort"), "default_reasoning_effort", 40) or None,
        "provisioning_prompt": _clean_text(
            payload.get("provisioning_prompt"),
            "provisioning_prompt",
            MAX_PROVISIONING_PROMPT_CHARS,
        ) or None,
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    if profile["base_profile_revision"] and not profile["base_profile_id"]:
        raise HarnessProfileError("base_profile_revision requires base_profile_id")
    if profile["base_profile_id"] == profile_id:
        raise HarnessProfileError("a harness profile cannot base on itself")
    profile["revision"] = _revision(profile)
    return profile


def _revision(profile: dict[str, Any]) -> str:
    data = copy.deepcopy(profile)
    data.pop("revision", None)
    data.pop("updated_at", None)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:16]


def _input_payload_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the mutable normalize-input shape from a stored profile so
    a partial edit re-runs the same normalization instead of hand-copying
    every field at each write site."""
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "description": profile.get("description"),
        "source": profile.get("source"),
        "overrides": profile.get("overrides"),
        "mcp_overrides": profile.get("mcp_overrides"),
        "skill_overrides": profile.get("skill_overrides"),
        "native_harness_overrides": profile.get("native_harness_overrides"),
        "provider_run_config_overlay": profile.get("provider_run_config_overlay"),
        "capability_contexts": profile.get("capability_contexts"),
        "secret_refs": profile.get("secret_refs"),
        "base_profile_id": profile.get("base_profile_id"),
        "base_profile_revision": profile.get("base_profile_revision"),
        "default_runtime_profile_id": profile.get("default_runtime_profile_id"),
        "default_model": profile.get("default_model"),
        "default_reasoning_effort": profile.get("default_reasoning_effort"),
        "provisioning_prompt": profile.get("provisioning_prompt"),
    }


def _assert_base_chain_ok(profile_id: str, profile: dict[str, Any], profiles: dict[str, Any]) -> None:
    """Reject a base chain that cycles, names a missing profile, or pins a
    base revision that is not the base's current revision. Runs at every save
    so an author is stopped at the edit, not at some later resolve.

    `profile` is the not-yet-persisted record for `profile_id`; the walk seeds
    from its base so a fresh A->A / A->B->A cycle is caught even though the new
    base pointer is not in `profiles` yet.
    """
    base_id = profile.get("base_profile_id")
    if not base_id:
        return
    chain = [profile_id]
    current_id = base_id
    current_rev = profile.get("base_profile_revision")
    while current_id and current_id != DEFAULT_PROFILE_ID:
        if current_id in chain:
            raise HarnessProfileError(
                "harness profile base chain cycle: " + " -> ".join(chain + [current_id])
            )
        chain.append(current_id)
        base = profiles.get(current_id)
        if not base:
            raise HarnessProfileError(f"harness profile base not found: {current_id}")
        if current_rev and base.get("revision") != current_rev:
            raise HarnessProfileError(f"harness profile base revision unavailable: {current_id}@{current_rev}")
        current_id = base.get("base_profile_id")
        current_rev = base.get("base_profile_revision")


def _commit_profile(data: dict[str, Any], profile_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Single write choke point: validate the base chain against current
    store state, persist, and return a copy."""
    _assert_base_chain_ok(profile_id, profile, data["profiles"])
    _write_profile_row(profile)
    return copy.deepcopy(profile)


def _extension_profiles() -> list[dict[str, Any]]:
    import extension_store
    raw_profiles = extension_store.extension_harness_profiles()
    profiles: list[dict[str, Any]] = []
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            continue
        profile_id = _clean_id(raw.get("id"))
        profile = _normalized_payload(profile_id, raw)
        profile["source"] = _clean_text(raw.get("source"), "source", 80)
        profile["extension_id"] = _clean_id(raw.get("extension_id"))
        profile["read_only"] = True
        profiles.append(profile)
    return profiles


def _extension_profile(profile_id: str, revision: str | None = None) -> dict[str, Any] | None:
    for profile in _extension_profiles():
        if profile.get("id") != profile_id:
            continue
        if revision and profile.get("revision") != revision:
            return None
        return copy.deepcopy(profile)
    return None


_META_FIELDS = (
    "base_profile_id",
    "base_profile_revision",
    "default_runtime_profile_id",
    "default_model",
    "default_reasoning_effort",
    "provisioning_prompt",
)


def set_profile_meta(profile_id: str, patch: dict[str, Any], revision: str | None = None) -> dict[str, Any]:
    """Typed setter for a profile's scalar meta fields (base pointer + the
    provider/model/reasoning-effort pins). Only keys present in `patch` are
    changed; the base chain is re-validated before the write."""
    if not isinstance(patch, dict):
        raise HarnessProfileError("meta patch must be an object")
    unknown = set(patch) - set(_META_FIELDS)
    if unknown:
        raise HarnessProfileError(f"unknown meta fields: {sorted(unknown)}")
    with _LOCK:
        data = _load()
        clean_id = _clean_id(profile_id)
        if clean_id == DEFAULT_PROFILE_ID:
            raise HarnessProfileError("the default profile is not a stored profile")
        existing = data["profiles"].get(clean_id)
        if not existing:
            raise HarnessProfileNotFoundError("harness profile not found")
        if revision and existing.get("revision") != revision:
            raise HarnessProfileError("Harness profile changed; reload before editing")
        payload = _input_payload_from_profile(existing)
        for key, value in patch.items():
            payload[key] = value
        profile = _normalized_payload(clean_id, payload, existing=existing)
        return _commit_profile(data, clean_id, profile)


def list_profiles() -> list[dict[str, Any]]:
    with _LOCK:
        profiles = list(_load()["profiles"].values())
    profiles.extend(_extension_profiles())
    return sorted((copy.deepcopy(p) for p in profiles), key=lambda p: (p.get("name") or "", p.get("id") or ""))


def get_profile(profile_id: str, revision: str | None = None) -> dict[str, Any] | None:
    clean_id = _clean_id(profile_id)
    if clean_id == DEFAULT_PROFILE_ID:
        raise HarnessProfileError("the default profile is not a stored profile")
    with _LOCK:
        profile = _load()["profiles"].get(clean_id)
    if not profile:
        return _extension_profile(clean_id, revision)
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
        if _extension_profile(clean_id):
            raise HarnessProfileError("harness profile id is owned by an extension")
        existing = data["profiles"].get(clean_id)
        profile = _normalized_payload(clean_id, payload, existing=existing)
        return _commit_profile(data, clean_id, profile)


def _id_from_name(value: object) -> str:
    text = str(value or "harness profile").strip().lower()
    clean = re.sub(r"[^a-z0-9_.-]+", "-", text).strip(".-")
    clean = re.sub(r"[-_.]{2,}", "-", clean)
    if len(clean) < 3:
        clean = "harness-profile"
    return clean[:76]


def _unique_id_from_name(value: object, *, existing_ids: set[str]) -> str:
    base = _id_from_name(value)
    if base not in existing_ids:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"[:80]
        if candidate not in existing_ids:
            return candidate
    raise HarnessProfileError("Could not derive a unique harness profile id from name")


def create_profile(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HarnessProfileError("Harness profile payload must be an object")
    with _LOCK:
        profile_id = _clean_id(payload.get("id") or None, required=False)
        extension_ids = {profile["id"] for profile in _extension_profiles()}
        if not profile_id:
            existing_ids = set(_load()["profiles"].keys()) | extension_ids | {DEFAULT_PROFILE_ID}
            profile_id = _unique_id_from_name(payload.get("name"), existing_ids=existing_ids)
        elif profile_id in extension_ids:
            raise HarnessProfileError("harness profile id is owned by an extension")
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
    "extension_instances",
    "disabled_builtin_tools",
    "disabled_builtin_extensions",
    "disabled_runtime_skills",
    "instruction_sources",
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
            raise HarnessProfileNotFoundError("harness profile not found")
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
        payload = _input_payload_from_profile(existing)
        payload["overrides"] = overrides
        profile = _normalized_payload(clean_id, payload, existing=existing)
        return _commit_profile(data, clean_id, profile)


def export_harness_sync_state() -> dict[str, Any]:
    """Harness profile state to project onto an approved worker node.

    Profiles hold only opaque `secret_refs` token references, never secret
    values, so the whole set is safe to copy.
    """
    with _LOCK:
        data = _load()
        return {
            "schema_version": SCHEMA_VERSION,
            "profiles": copy.deepcopy(data.get("profiles") or {}),
        }


def import_harness_sync_state(state: object) -> dict[str, Any]:
    """Replace this host's profiles with the primary's projection.

    Schema migrations are not supported, so a payload from a differently
    versioned primary is refused rather than partially applied.
    """
    if not isinstance(state, dict):
        raise HarnessProfileError("harness sync state must be an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise HarnessProfileError(
            "harness sync state is incompatible with this Better Agent version"
        )
    profiles = state.get("profiles")
    if not isinstance(profiles, dict):
        raise HarnessProfileError("harness sync state profiles must be an object")
    with _LOCK:
        _replace_all_profiles(copy.deepcopy(profiles))
    return {"profiles": len(profiles)}


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
        _delete_profile_row(clean_id)
        return True
