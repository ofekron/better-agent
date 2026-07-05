from __future__ import annotations

import copy
from contextlib import contextmanager
import gzip
import io
import re
import shutil
import subprocess
import tempfile
import threading
import time
import os
import json
import sys
import base64
import hashlib
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from env_compat import dual_env_many, get_env
from json_store import read_json, write_json
from paths import ba_home
import password_manager
import extension_applied_config
import extension_instructions
import extension_mcp

STORE_SCHEMA_VERSION = 2
MANIFEST_KIND = "better-agent-extension"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]{0,127}$")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_GIT_SCP_RE = re.compile(r"^git@[A-Za-z0-9_.-]+:[A-Za-z0-9_.~/-]+\.git$")
_ALLOWED_SURFACES = {"backend_feature", "frontend_feature", "runtime_mcp", "instructions", "skills"}
# Scope an instruction section is injected at. "global" -> the provider's home
# instruction file (~/.claude/CLAUDE.md); "project" -> the project-root file.
_INSTRUCTION_LEVELS = {"global", "project"}
# How a frontend_modules entry is rendered by the host. "module" = dynamically
# imported JS module mounted into the slot; "iframe" = the HTML asset embedded
# in an <iframe> filling the slot.
_FRONTEND_MODULE_KINDS = {"module", "iframe"}
_RUNTIME_SKILL_OWNER_FILE = ".better-agent-extension-owner"
_HARNESS_DELIVERY_NATIVE = "native"
_HARNESS_DELIVERY_RUNTIME = "runtime"
_HARNESS_DELIVERY_MODES = {_HARNESS_DELIVERY_NATIVE, _HARNESS_DELIVERY_RUNTIME}
_PRIVATE_LOCAL_RUNTIME_MODE_ENV = "BETTER_AGENT_PRIVATE_EXTENSION_RUNTIME"
_PRIVATE_LOCAL_RUNTIME_SOURCE = "source"
_PRIVATE_LOCAL_RUNTIME_PACKAGED = "packaged"
_PROJECTION_CACHE: dict[tuple[str, tuple[Any, ...]], Any] = {}
_ENABLED_CACHE: dict[str, tuple[tuple[int, int], bool]] = {}
_ENABLED_CACHE_LOCK = threading.Lock()
# Fingerprint-keyed cache for get_extension() — defined here (beside the
# other store caches) so _clear_projection_cache can reference it.
_GET_EXTENSION_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any] | None]] = {}
_GET_EXTENSION_CACHE_LOCK = threading.Lock()
_BUILTIN_FEATURE_CACHE: dict[str, tuple[tuple[int, int], bool]] = {}
_BUILTIN_FEATURE_CACHE_LOCK = threading.Lock()
_STORE_FINGERPRINT_CACHE: tuple[float, tuple[int, int]] | None = None
_STORE_FINGERPRINT_CACHE_LOCK = threading.Lock()
_STORE_FINGERPRINT_TTL_SECONDS = 0.5
_RECONCILED_STORE_FINGERPRINT: tuple[str, tuple[int, int]] | None = None
_RECONCILED_STORE_LOCK = threading.Lock()
_RESERVED_MCP_SERVER_NAMES = {
    "browser-harness",
    "canvas",
    "capabilities",
    "communicate",
    "create-worker",
    "credential-broker",
    "get-requirements",
    "handoff",
    "open-config-panel",
    "project-updates",
    "ui",
    "provider-config-sync",
    "better-agent-coordination",
    "session-bridge",
}

def _load_private_builtin_registry() -> dict[str, Any]:
    """Load private/commercial extension ids + metadata from the private
    checkout (gitignored from this public repo).

    The registry is a static property of the SOURCE TREE (the better-agent-private
    sibling), not of the runtime marketplace repo path — tests legitimately
    point BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH at a temp root to isolate
    extension *packages*, and they must not lose the builtin *ids*. So: prefer an
    env-configured root that actually contains the registry, else fall back to
    the source-tree sibling. Returns empty maps when the private checkout is
    absent (pure-public) — private BUILTIN_* ids then resolve to None and every
    gate referencing them fails closed. No private id string lives here.
    """
    import importlib.util

    empty = {"ids": {}, "paths": {}, "llm_tasks": {}, "llm_task_labels": {},
             "mcp_replacements": {}, "runtime_required_paths": {},
             "display_names": {}}
    if os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") == "1":
        return empty
    candidates: list[Path] = []
    configured = str(os.environ.get("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser().resolve())
    here = Path(__file__).resolve().parent
    candidates.append((here.parent / "better-agent-private").resolve())
    for root in candidates:
        path = root / "private_builtin_ids.py"
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_private_builtin_ids", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        except Exception:
            continue
        return {
            "ids": getattr(module, "PRIVATE_BUILTIN_IDS", {}),
            "paths": getattr(module, "PRIVATE_EXTENSION_DIRS", {}),
            "llm_tasks": getattr(module, "INTERNAL_LLM_TASKS", {}),
            "llm_task_labels": getattr(module, "INTERNAL_LLM_TASK_LABELS", {}),
            "mcp_replacements": getattr(module, "MCP_REPLACEMENTS", {}),
            "runtime_required_paths": getattr(module, "RUNTIME_REQUIRED_PATHS", {}),
            "display_names": getattr(module, "DISPLAY_NAMES", {}),
        }
    return empty


_PRIVATE_REGISTRY = _load_private_builtin_registry()
_PRIV_IDS = _PRIVATE_REGISTRY["ids"]


def _pid(key: str) -> str | None:
    """Real extension id for a private logical key, or None when the private
    checkout is absent (fail closed)."""
    return _PRIV_IDS.get(key)


# Public builtin ids stay literal in the public repo.
BUILTIN_ASK_EXTENSION_ID = "ofek-dev.ask"
BUILTIN_SESSION_BRIDGE_EXTENSION_ID = "ofek-dev.session-bridge"
BUILTIN_SESSION_CONTROL_EXTENSION_ID = "ofek-dev.session-control"
BUILTIN_COORDINATION_EXTENSION_ID = "ofek-dev.coordination"
BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID = "ofek-dev.provider-config-sync"
BUILTIN_TODOS_EXTENSION_ID = "ofek-dev.todos"
BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID = "better-agent.harness-for-better-agent"
BUILTIN_USER_ATTENTION_EXTENSION_ID = "ofek-dev.user-attention"
# Private/commercial ids resolve from the gitignored private registry; None in
# pure-public. The real id strings never appear in this public module.
BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID = _pid("team_orchestration")
BUILTIN_SUPERVISOR_EXTENSION_ID = _pid("supervisor")
BUILTIN_REQUIREMENTS_EXTENSION_ID = _pid("requirements")
BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID = _pid("project_structure")
BUILTIN_MACHINE_NODES_EXTENSION_ID = _pid("machine_nodes")
BUILTIN_CREDENTIAL_BROKER_EXTENSION_ID = _pid("credential_broker")
BUILTIN_CANVAS_EXTENSION_ID = _pid("canvas")
BUILTIN_TRACE_INSPECTOR_EXTENSION_ID = _pid("trace_inspector")
BUILTIN_REARRANGER_EXTENSION_ID = _pid("rearranger")
BUILTIN_PROMPT_ENGINEER_EXTENSION_ID = _pid("prompt_engineer")
BUILTIN_BROWSER_HARNESS_EXTENSION_ID = _pid("browser_harness")
BUILTIN_AGENT_BOARD_EXTENSION_ID = _pid("agent_board")
BUILTIN_TESTAPE_EXTENSION_ID = _pid("testape")
BUILTIN_SCHEDULER_EXTENSION_ID = _pid("scheduler")
BUILTIN_TASKS_EXTENSION_ID = _pid("tasks")
BUILTIN_ASSISTANT_EXTENSION_ID = _pid("assistant")
BUILTIN_ADV_EXTENSION_ID = _pid("adv")
_BUILTIN_MCP_REPLACEMENTS_BY_EXTENSION_ID = {
    **{_pid(k): v for k, v in _PRIVATE_REGISTRY["mcp_replacements"].items() if _pid(k)},
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: frozenset({"provider-config-sync"}),
    BUILTIN_COORDINATION_EXTENSION_ID: frozenset({"better-agent-coordination"}),
}
MARKETPLACE_EXTENSION_ID = "ofek-dev.marketplace"
REQUIRED_EXTENSION_IDS = {MARKETPLACE_EXTENSION_ID}
PUBLIC_EXTENSION_LIST_HIDDEN_IDS = frozenset()
_OBSOLETE_EXTENSION_IDS = {
    "better-agent.marketplace": MARKETPLACE_EXTENSION_ID,
    "ofek-dev.needs-user-decision": BUILTIN_USER_ATTENTION_EXTENSION_ID,
}
_PRIVATE_EXTENSION_PATHS = {
    **{_pid(k): v for k, v in _PRIVATE_REGISTRY["paths"].items() if _pid(k)},
    MARKETPLACE_EXTENSION_ID: "extensions/marketplace",
}
_PUBLIC_EXTENSION_PATHS = {
    BUILTIN_ASK_EXTENSION_ID: "extensions/ask",
    BUILTIN_SESSION_BRIDGE_EXTENSION_ID: "extensions/session-bridge",
    BUILTIN_SESSION_CONTROL_EXTENSION_ID: "extensions/session-control",
    BUILTIN_COORDINATION_EXTENSION_ID: "extensions/coordination",
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: "extensions/provider-config-sync",
    BUILTIN_TODOS_EXTENSION_ID: "extensions/todos",
    BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: "extensions/harness-instructions",
    BUILTIN_USER_ATTENTION_EXTENSION_ID: "extensions/user-attention",
}
_PRIVATE_EXTENSION_NAMES = {
    BUILTIN_ASK_EXTENSION_ID: "Ask",
    BUILTIN_SESSION_BRIDGE_EXTENSION_ID: "Session Bridge",
    BUILTIN_SESSION_CONTROL_EXTENSION_ID: "Session Control",
    BUILTIN_COORDINATION_EXTENSION_ID: "Coordination",
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: "Provider Config Sync",
    BUILTIN_TODOS_EXTENSION_ID: "Todos",
    BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: "Harness instructions",
    BUILTIN_USER_ATTENTION_EXTENSION_ID: "User attention",
    MARKETPLACE_EXTENSION_ID: "Marketplace",
    **{_pid(k): v for k, v in _PRIVATE_REGISTRY["display_names"].items() if _pid(k)},
}
_DEFAULT_MARKETPLACE_BASE_URL = "https://ofek-dev.com/api/marketplace"
_DEFAULT_MARKETPLACE_PUBLIC_KEY = "a61a192e23f0f0898fa096ae64e0d22d853eb0701e2c94a6d55fff7b2f52b7fd"
_MARKETPLACE_USER_AGENT = "BetterAgentMarketplace/1.0"
_MARKETPLACE_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+:/#-]{0,119}$")
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
_required_artifact_update_checked: set[str] = set()

_BUILTIN_INTERNAL_LLM_TASKS: dict[str, tuple[str, ...]] = {
    BUILTIN_ASK_EXTENSION_ID: ("session_search_worker",),
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: ("provider_config_sync_review",),
    BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: ("extension_context_audit",),
    **{_pid(k): v for k, v in _PRIVATE_REGISTRY["llm_tasks"].items() if _pid(k)},
}
_EXTENSION_SETTINGS_INTERNAL_LLM_TASKS: dict[str, tuple[str, ...]] = {
    **(
        {BUILTIN_ASK_EXTENSION_ID: ("session_search_worker",)}
        if BUILTIN_ASK_EXTENSION_ID
        else {}
    ),
    **(
        {BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: ("provider_config_sync_review",)}
        if BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID
        else {}
    ),
    **(
        {
            BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID: (
                "delegation_task",
                "delegation_message",
                "delegation_ask",
            )
        }
        if BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
        else {}
    ),
    **(
        {BUILTIN_SESSION_BRIDGE_EXTENSION_ID: ("delegation_session_bridge",)}
        if BUILTIN_SESSION_BRIDGE_EXTENSION_ID
        else {}
    ),
    **(
        {BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: ("extension_context_audit",)}
        if BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID
        else {}
    ),
    **{
        _pid(k): v
        for k, v in _PRIVATE_REGISTRY["llm_tasks"].items()
        if _pid(k) and v != ("default_session",)
    },
}
_BUILTIN_RUNTIME_REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    **{_pid(k): v for k, v in _PRIVATE_REGISTRY["runtime_required_paths"].items() if _pid(k)},
}

# Frontend-facing logical key -> resolved extension id. Private ids are absent
# in a pure-public checkout (registry not loaded) and filtered out. The
# frontend fetches this so it never hardcodes private ids.
_FRONTEND_BUILTIN_KEYS = {
    "ask": BUILTIN_ASK_EXTENSION_ID,
    "team": BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID,
    "supervisor": BUILTIN_SUPERVISOR_EXTENSION_ID,
    "projectStructure": BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID,
    "machineNodes": BUILTIN_MACHINE_NODES_EXTENSION_ID,
    "credentialBroker": BUILTIN_CREDENTIAL_BROKER_EXTENSION_ID,
    "providerConfigSync": BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID,
    "canvas": BUILTIN_CANVAS_EXTENSION_ID,
    "rearranger": BUILTIN_REARRANGER_EXTENSION_ID,
    "promptEngineer": BUILTIN_PROMPT_ENGINEER_EXTENSION_ID,
    "browserHarness": BUILTIN_BROWSER_HARNESS_EXTENSION_ID,
    "agentBoard": BUILTIN_AGENT_BOARD_EXTENSION_ID,
    "traceInspector": BUILTIN_TRACE_INSPECTOR_EXTENSION_ID,
    "requirements": BUILTIN_REQUIREMENTS_EXTENSION_ID,
    "sessionBridge": BUILTIN_SESSION_BRIDGE_EXTENSION_ID,
    "testape": BUILTIN_TESTAPE_EXTENSION_ID,
    "scheduler": BUILTIN_SCHEDULER_EXTENSION_ID,
    "tasks": BUILTIN_TASKS_EXTENSION_ID,
    "assistant": BUILTIN_ASSISTANT_EXTENSION_ID,
}


def builtin_extension_id_map() -> dict[str, str]:
    """Logical key -> resolved extension id for known builtins, with private
    ids dropped when the private registry isn't loaded (pure-public)."""
    return {k: v for k, v in _FRONTEND_BUILTIN_KEYS.items() if v}


class ExtensionError(ValueError):
    pass


class ExtensionConsentRequired(ExtensionError):
    """Raised when a non-builtin extension is enabled before the user has
    consented to its declared permission set (trusted-by-install model)."""
    pass


_STORE_PATH: Path | None = None


def _store_path() -> Path:
    global _STORE_PATH
    if _STORE_PATH is None:
        _STORE_PATH = ba_home() / "extensions" / "extensions.json"
    return _STORE_PATH


def store_fingerprint() -> tuple[int, int]:
    global _STORE_FINGERPRINT_CACHE
    now = time.monotonic()
    with _STORE_FINGERPRINT_CACHE_LOCK:
        cached = _STORE_FINGERPRINT_CACHE
        if (
            cached is not None
            and now - cached[0] <= _STORE_FINGERPRINT_TTL_SECONDS
        ):
            return cached[1]
    path = _store_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        fingerprint = (0, 0)
    else:
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        _STORE_FINGERPRINT_CACHE = (now, fingerprint)
    return fingerprint


def _refresh_store_fingerprint_cache(path: Path | None = None) -> tuple[int, int]:
    global _STORE_FINGERPRINT_CACHE
    path = path or _store_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        fingerprint = (0, 0)
    else:
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        _STORE_FINGERPRINT_CACHE = (time.monotonic(), fingerprint)
    return fingerprint


def _file_fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _projection_cache_get(name: str, key: tuple[Any, ...]) -> Any:
    cached = _PROJECTION_CACHE.get((name, key))
    return copy.deepcopy(cached) if cached is not None else None


def _projection_cache_put(name: str, key: tuple[Any, ...], value: Any) -> Any:
    _PROJECTION_CACHE[(name, key)] = copy.deepcopy(value)
    return copy.deepcopy(value)


def _projection_cache_items(name: str) -> list[tuple[tuple[Any, ...], Any]]:
    prefix = (name,)
    return [
        (key[1], copy.deepcopy(value))
        for key, value in _PROJECTION_CACHE.items()
        if key[:1] == prefix
    ]


def _clear_projection_cache() -> None:
    global _RECONCILED_STORE_FINGERPRINT
    _PROJECTION_CACHE.clear()
    with _RECONCILED_STORE_LOCK:
        _RECONCILED_STORE_FINGERPRINT = None
    # get_extension's fingerprint cache auto-invalidates on any store write
    # (file mtime/size changes), but a same-fingerprint forced refresh must
    # drop it too so a reconcile that rewrites identical bytes is observed.
    with _GET_EXTENSION_CACHE_LOCK:
        _GET_EXTENSION_CACHE.clear()


def _install_root() -> Path:
    return ba_home() / "extensions" / "installed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank_store() -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "extensions": {},
        "deleted_extensions": {},
    }


@contextmanager
def _store_lock():
    lock_path = ba_home() / "extensions" / "extensions.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


_SOURCE_TYPE_V1_TO_V2 = {
    "public_builtin": "better_agent_bundled",
    "private_local": "better_agent_local",
    "required_artifact": "better_agent_signed",
}


def _migrate_store_v1_to_v2(data: dict[str, Any]) -> None:
    for record in (data.get("extensions") or {}).values():
        source = record.get("source") if isinstance(record, dict) else None
        if not isinstance(source, dict):
            continue
        new_type = _SOURCE_TYPE_V1_TO_V2.get(source.get("type"))
        if new_type:
            source["type"] = new_type
    data["schema_version"] = 2


def _read_store_unlocked() -> dict[str, Any]:
    data = read_json(_store_path(), _blank_store())
    if data.get("schema_version") == 1:
        _migrate_store_v1_to_v2(data)
        _write_store_unlocked(data)
    if data.get("schema_version") != STORE_SCHEMA_VERSION:
        raise ExtensionError("Unsupported extension store schema; wipe extensions/extensions.json to start fresh")
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        raise ExtensionError("Malformed extension store: extensions must be an object")
    if not isinstance(data.get("deleted_extensions"), dict):
        data["deleted_extensions"] = {}
    return data


def _write_store_unlocked(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".extensions.",
        suffix=".tmp",
        delete=False,
    ) as fh:
        json.dump(data, fh, indent=2)
        tmp_name = fh.name
    os.replace(tmp_name, path)
    _refresh_store_fingerprint_cache(path)
    _clear_projection_cache()


def _merge_store_for_save(
    current: dict[str, Any],
    next_data: dict[str, Any],
    *,
    deleted_extension_ids: set[str],
    resurrect_extension_ids: set[str],
) -> dict[str, Any]:
    deleted = dict(current.get("deleted_extensions") or {})
    for extension_id in deleted_extension_ids:
        deleted[extension_id] = _now()
    for extension_id in resurrect_extension_ids:
        deleted.pop(extension_id, None)
    merged = {**next_data, "extensions": dict(current.get("extensions") or {})}
    managed_ids = {*_PUBLIC_EXTENSION_PATHS, *_PRIVATE_EXTENSION_PATHS}
    for extension_id in deleted_extension_ids:
        merged["extensions"].pop(extension_id, None)
    for extension_id, record in (next_data.get("extensions") or {}).items():
        if (
            extension_id in deleted
            and extension_id not in resurrect_extension_ids
            and extension_id not in managed_ids
        ):
            continue
        merged["extensions"][extension_id] = record
    for extension_id in deleted:
        if extension_id not in resurrect_extension_ids and extension_id not in managed_ids:
            merged["extensions"].pop(extension_id, None)
    merged["deleted_extensions"] = deleted
    return merged


def _load_with_changes() -> tuple[dict[str, Any], bool, bool]:
    with _store_lock():
        data = _read_store_unlocked()
        changed, public_changed = _reconcile_loaded_store(data)
        if changed:
            _write_store_unlocked(data)
        return data, changed, public_changed


# Each private-repo HEAD commit and each public package-hash change produces a
# new version snapshot dir under <install_root>/<id>/versions/. The active
# version (the one referenced by the live record's install_path) is always
# kept; this many most-recent prior snapshots are kept as fallbacks for
# in-flight processes launched against an older version. Older ones are GC'd.
_MAX_FALLBACK_VERSIONS = 3


def _prune_extension_versions(data: dict[str, Any]) -> None:
    """Delete stale on-disk version snapshots for every installed extension.

    Pure disk GC — does not mutate store state. The active install_path is
    always retained; among the remaining version dirs the N newest by mtime
    are kept, the rest removed. Fails open per-dir so one broken entry never
    blocks reconcile. Never deletes outside the extension's versions/ dir.
    """
    root = _install_root().resolve()
    for extension_id, record in (data.get("extensions") or {}).items():
        versions_dir = root / extension_id / "versions"
        if not versions_dir.is_dir():
            continue
        try:
            versions_resolved = versions_dir.resolve()
            dirs = [p for p in versions_dir.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            continue
        active = Path(str((record.get("source") or {}).get("install_path") or "")).resolve()
        fallbacks: list[Path] = []
        for p in dirs:
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if resolved == active or not resolved.is_relative_to(versions_resolved):
                continue
            fallbacks.append(p)
        if len(fallbacks) <= _MAX_FALLBACK_VERSIONS:
            continue
        fallbacks.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in fallbacks[_MAX_FALLBACK_VERSIONS:]:
            shutil.rmtree(stale, ignore_errors=True)


def _reconcile_loaded_store(data: dict[str, Any]) -> tuple[bool, bool]:
    changed = False
    public_changed = False
    if data.pop("builtin_extensions_seeded", None) is not None:
        changed = True
    if _purge_obsolete_extension_records(data):
        changed = True
    if _rehydrate_installed_extension_records(data):
        changed = True
    if _ensure_public_extensions(data):
        changed = True
        public_changed = True
    if _ensure_private_extensions(data):
        changed = True
    _prune_extension_versions(data)
    return changed, public_changed


def _load() -> dict[str, Any]:
    with _store_lock():
        return _read_store_unlocked()


def _save(
    data: dict[str, Any],
    *,
    deleted_extension_ids: set[str] | None = None,
    resurrect_extension_ids: set[str] | None = None,
) -> None:
    with _store_lock():
        current = _read_store_unlocked()
        merged = _merge_store_for_save(
            current,
            data,
            deleted_extension_ids=set(deleted_extension_ids or ()),
            resurrect_extension_ids=set(resurrect_extension_ids or ()),
        )
        _write_store_unlocked(merged)


def _safe_sync_artifact_name(extension_id: str) -> str:
    if not _ID_RE.fullmatch(extension_id or ""):
        raise ExtensionError(f"invalid extension id in sync payload: {extension_id!r}")
    return extension_id


def _extension_record_sync_copy(record: dict[str, Any]) -> dict[str, Any]:
    clean = copy.deepcopy(record)
    source = clean.get("source")
    if isinstance(source, dict):
        # install_path is machine-local. The sync importer rewrites it after
        # unpacking the active package snapshot into this node's BA home.
        source.pop("install_path", None)
    return clean


def _settings_without_secret_values(
    settings: dict[str, Any],
    extensions: dict[str, Any],
) -> dict[str, Any]:
    clean = copy.deepcopy(settings)
    entries = clean.get("extensions")
    if not isinstance(entries, dict):
        return clean
    for extension_id, entry in list(entries.items()):
        if not isinstance(entry, dict):
            continue
        values = entry.get("values")
        if not isinstance(values, dict):
            continue
        record = extensions.get(extension_id)
        manifest = record.get("manifest") if isinstance(record, dict) else {}
        setting_schema = ((manifest or {}).get("entrypoints") or {}).get("settings") or []
        secret_keys = {
            item.get("key")
            for item in setting_schema
            if isinstance(item, dict) and item.get("type") == "secret"
        }
        for key in secret_keys:
            values.pop(key, None)
    return clean


def export_extension_sync_state() -> dict[str, Any]:
    """Extension state safe to copy to an approved worker node.

    The payload includes the JSON store, UI/settings sidecars, and active
    installed package snapshots. Secret setting values are never exported:
    extension_store keeps them in the OS keychain, and the settings sidecar is
    scrubbed defensively in case a legacy/plain value ever existed.
    """
    data, _changed, _public_changed = _load_with_changes()
    extensions = {
        extension_id: _extension_record_sync_copy(record)
        for extension_id, record in (data.get("extensions") or {}).items()
        if isinstance(record, dict)
    }
    artifacts: list[dict[str, Any]] = []
    for extension_id, record in (data.get("extensions") or {}).items():
        if not isinstance(record, dict):
            continue
        source = record.get("source") or {}
        install_path = Path(str(source.get("install_path") or ""))
        if not install_path.is_dir():
            continue
        archive = _build_package_artifact(install_path)
        artifact_sha256 = hashlib.sha256(archive).hexdigest()
        artifacts.append({
            "extension_id": extension_id,
            "archive_b64": base64.b64encode(archive).decode("ascii"),
            "artifact_sha256": artifact_sha256,
            "commit_sha": str(source.get("commit_sha") or artifact_sha256),
        })
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "store": {
            "schema_version": STORE_SCHEMA_VERSION,
            "extensions": extensions,
            "deleted_extensions": copy.deepcopy(data.get("deleted_extensions") or {}),
        },
        "extension_settings": _settings_without_secret_values(
            _load_ext_settings(),
            data.get("extensions") or {},
        ),
        "ui_settings": {
            "schema_version": _UI_SETTINGS_SCHEMA_VERSION,
            "settings": copy.deepcopy(_load_ui_settings()),
        },
        "artifacts": artifacts,
    }


def _install_synced_artifact(artifact: dict[str, Any], records: dict[str, Any]) -> None:
    extension_id = _safe_sync_artifact_name(str(artifact.get("extension_id") or ""))
    record = records.get(extension_id)
    if not isinstance(record, dict):
        raise ExtensionError(f"sync artifact references unknown extension: {extension_id}")
    archive_b64 = str(artifact.get("archive_b64") or "")
    expected_sha = str(artifact.get("artifact_sha256") or "").strip().lower()
    try:
        archive = base64.b64decode(archive_b64, validate=True)
    except ValueError as exc:
        raise ExtensionError(f"sync artifact for {extension_id} is not valid base64") from exc
    actual_sha = hashlib.sha256(archive).hexdigest()
    if expected_sha and expected_sha != actual_sha:
        raise ExtensionError(f"sync artifact sha mismatch for {extension_id}")
    source = record.setdefault("source", {})
    version_key = str(
        artifact.get("commit_sha")
        or source.get("commit_sha")
        or source.get("artifact_sha256")
        or actual_sha
    ).strip() or actual_sha
    version_key = re.sub(r"[^A-Za-z0-9_.+-]", "_", version_key)[:128] or actual_sha
    target = _install_root() / extension_id / "versions" / version_key
    if target.exists():
        shutil.rmtree(target)
    _safe_extract_tar_gz(archive, target)
    manifest_path = target / "better-agent-extension.json"
    if not manifest_path.is_file():
        shutil.rmtree(target, ignore_errors=True)
        raise ExtensionError(f"sync artifact for {extension_id} missing manifest")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest["id"] != extension_id:
        shutil.rmtree(target, ignore_errors=True)
        raise ExtensionError(f"sync artifact id mismatch for {extension_id}")
    _validate_declared_files(manifest, target)
    _install_python_requirements(target, manifest)
    record["manifest"] = manifest
    record["smoke_test"] = _run_extension_smoke_test(manifest, target)
    source["install_path"] = str(target)
    source.setdefault("commit_sha", version_key)
    source["synced_artifact_sha256"] = actual_sha
    record["source"] = source


def _reconcile_after_sync(records: dict[str, Any]) -> dict[str, int]:
    instruction_swept = reconcile_all_instructions()
    skill_changes = reconcile_runtime_skills()
    mcp_changes = reconcile_native_mcp_servers()
    token_changes = reconcile_extension_tokens()
    consent_changes = reconcile_extension_consent()
    for extension_id in records:
        try:
            record = get_extension(extension_id)
            if record:
                extension_applied_config.reconcile(record)
                _evict_extension_backend(extension_id)
        except Exception:
            pass
    return {
        "instruction_swept": int(instruction_swept or 0),
        "runtime_skill_changes": int(skill_changes or 0),
        "native_mcp_changes": int(mcp_changes or 0),
        "token_changes": int(token_changes or 0),
        "consent_changes": int(consent_changes or 0),
    }


def import_extension_sync_state(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExtensionError("extension sync payload must be an object")
    store = payload.get("store")
    if not isinstance(store, dict):
        raise ExtensionError("extension sync payload must include store")
    if store.get("schema_version") != STORE_SCHEMA_VERSION:
        raise ExtensionError("extension sync store schema is unsupported")
    raw_extensions = store.get("extensions")
    if not isinstance(raw_extensions, dict):
        raise ExtensionError("extension sync store extensions must be an object")
    records = {
        _safe_sync_artifact_name(str(extension_id)): copy.deepcopy(record)
        for extension_id, record in raw_extensions.items()
        if isinstance(record, dict)
    }
    for record in records.values():
        source = record.get("source")
        if isinstance(source, dict):
            source.pop("install_path", None)
    for artifact in payload.get("artifacts") or []:
        if not isinstance(artifact, dict):
            raise ExtensionError("extension sync artifacts must be objects")
        _install_synced_artifact(artifact, records)
    next_store = {
        "schema_version": STORE_SCHEMA_VERSION,
        "extensions": records,
        "deleted_extensions": copy.deepcopy(store.get("deleted_extensions") or {}),
    }
    with _store_lock():
        _write_store_unlocked(next_store)
    ext_settings = payload.get("extension_settings")
    if isinstance(ext_settings, dict):
        _save_ext_settings(_settings_without_secret_values(ext_settings, records))
    ui_settings = payload.get("ui_settings")
    if isinstance(ui_settings, dict):
        settings = ui_settings.get("settings")
        if isinstance(settings, dict):
            _save_ui_settings(copy.deepcopy(settings))
    reconcile = _reconcile_after_sync(records)
    return {
        "ok": True,
        "extension_count": len(records),
        "artifact_count": len(payload.get("artifacts") or []),
        "reconcile": reconcile,
    }


def _clean_rel_path(value: str, *, field: str) -> str:
    path = str(value or "").strip()
    if not path:
        raise ExtensionError(f"{field} is required")
    if not _REL_PATH_RE.fullmatch(path):
        raise ExtensionError(f"{field} contains invalid characters")
    rel = Path(path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ExtensionError(f"{field} must be a safe relative path")
    return rel.as_posix()


def _clean_optional_rel_path(value: Any, *, field: str) -> str:
    if value in (None, ""):
        return ""
    return _clean_rel_path(str(value), field=field)


def _validate_string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ExtensionError(f"{field} must be a string list")
    return [item.strip() for item in value]


_PYTHON_REQUIREMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-\[\],<>=!~;:'\" ]{0,255}$")
_PYTHON_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,20}$")
_EXTENSION_PROTOCOL_VERSION = 1


def _validate_python_requirements(value: Any) -> list[str]:
    requirements = _validate_string_list(value, field="entrypoints.python_requirements")
    for requirement in requirements:
        if not _PYTHON_REQUIREMENT_RE.fullmatch(requirement):
            raise ExtensionError("entrypoints.python_requirements contains an invalid requirement")
    return requirements


def _clean_optional_python_module(value: Any, *, field: str) -> str:
    if value in (None, ""):
        return ""
    module = str(value or "").strip()
    if not _PYTHON_MODULE_RE.fullmatch(module):
        raise ExtensionError(f"{field} must be a dotted Python module path")
    return module


def _validate_smoke_test(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ExtensionError("protocol.smoke_test must be an object")
    unknown = sorted(set(value) - {"required_paths", "python_modules"})
    if unknown:
        raise ExtensionError(f"protocol.smoke_test has unknown keys: {', '.join(unknown)}")
    required_paths = [
        _clean_rel_path(path, field="protocol.smoke_test.required_paths")
        for path in (_validate_string_list(value.get("required_paths"), field="protocol.smoke_test.required_paths") or ["better-agent-extension.json"])
    ]
    python_modules = [
        _clean_optional_python_module(module, field="protocol.smoke_test.python_modules")
        for module in _validate_string_list(value.get("python_modules"), field="protocol.smoke_test.python_modules")
    ]
    return {
        "required_paths": required_paths,
        "python_modules": python_modules,
    }


def _validate_protocol(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ExtensionError("protocol must be an object")
    unknown = sorted(set(value) - {"version", "smoke_test"})
    if unknown:
        raise ExtensionError(f"protocol has unknown keys: {', '.join(unknown)}")
    version = value.get("version", _EXTENSION_PROTOCOL_VERSION)
    if version != _EXTENSION_PROTOCOL_VERSION:
        raise ExtensionError(f"protocol.version must be {_EXTENSION_PROTOCOL_VERSION}")
    return {
        "version": _EXTENSION_PROTOCOL_VERSION,
        "smoke_test": _validate_smoke_test(value.get("smoke_test")),
    }


def _default_protocol_for_entrypoints(entrypoints: dict[str, Any]) -> dict[str, Any]:
    protocol = _validate_protocol(None)
    protocol["smoke_test"]["python_modules"] = _required_smoke_python_modules(entrypoints)
    return protocol


def _python_path_to_module(rel_path: str) -> str:
    path = Path(rel_path)
    if path.suffix != ".py":
        raise ExtensionError("Python entrypoint paths must end with .py")
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    module = ".".join(parts)
    return _clean_optional_python_module(module, field="entrypoints.mcp.python")


def _required_smoke_python_modules(entrypoints: dict[str, Any]) -> list[str]:
    modules: set[str] = set()
    backend_module = entrypoints.get("backend_module")
    if backend_module:
        modules.add(backend_module)
    for item in entrypoints.get("mcp") or []:
        module = item.get("module")
        if module:
            modules.add(module)
        python_path = item.get("python")
        if python_path:
            modules.add(_python_path_to_module(python_path))
    return sorted(modules)


def _validate_protocol_coverage(manifest: dict[str, Any]) -> None:
    required_modules = _required_smoke_python_modules(manifest["entrypoints"])
    declared_modules = set(manifest["protocol"]["smoke_test"]["python_modules"])
    missing = [module for module in required_modules if module not in declared_modules]
    if missing:
        raise ExtensionError(
            "protocol.smoke_test.python_modules must include declared Python entrypoints: "
            + ", ".join(missing)
        )


_DISALLOWED_REMOTE_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata",
})


def _is_disallowed_remote_host(hostname: str) -> bool:
    host = (hostname or "").strip().strip(".").lower()
    if not host:
        return True
    if host in _DISALLOWED_REMOTE_HOSTNAMES:
        return True
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_remote_services(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.remote_services must be a list")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.remote_services items must be objects")
        unknown = sorted(set(item) - {"name", "base_url", "purpose"})
        if unknown:
            raise ExtensionError(
                f"entrypoints.remote_services[{index}] has unknown keys: {', '.join(unknown)}"
            )
        name = str(item.get("name") or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ExtensionError("entrypoints.remote_services.name contains invalid characters")
        if name in seen:
            raise ExtensionError(f"entrypoints.remote_services contains duplicate name: {name}")
        seen.add(name)
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ExtensionError("entrypoints.remote_services.base_url must be https")
        if parsed.username or parsed.password:
            raise ExtensionError("entrypoints.remote_services.base_url must not embed credentials")
        if parsed.query or parsed.fragment:
            raise ExtensionError("entrypoints.remote_services.base_url must not include query or fragment")
        # Declarative hygiene: a manifest must not advertise an internal/private
        # SSRF target. This is NOT a runtime egress control — extension code runs
        # as a trusted subprocess (trusted-by-install model) and can reach any
        # host it wants; this only stops a published manifest from *declaring*
        # loopback/private/metadata endpoints as legitimate services.
        if _is_disallowed_remote_host(parsed.hostname or ""):
            raise ExtensionError(
                "entrypoints.remote_services.base_url must not target a private, "
                "loopback, link-local, or cloud-metadata host"
            )
        purpose = str(item.get("purpose") or "").strip()
        if not purpose:
            raise ExtensionError("entrypoints.remote_services.purpose is required")
        if len(purpose) > 240:
            raise ExtensionError("entrypoints.remote_services.purpose is too long")
        items.append({"name": name, "base_url": base_url, "purpose": purpose})
    return items


def _validate_instructions(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.instructions must be a list")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            raise ExtensionError(
                "entrypoints.instructions items must declare {name, path}; "
                f"item {index} only declared a name"
            )
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.instructions items must be objects")
        name = str(item.get("name") or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ExtensionError("entrypoints.instructions.name contains invalid characters")
        if name in seen:
            raise ExtensionError(f"entrypoints.instructions contains duplicate name: {name}")
        seen.add(name)
        path = _clean_rel_path(str(item.get("path") or ""), field="entrypoints.instructions.path")
        level = str(item.get("level") or "global").strip()
        if level not in _INSTRUCTION_LEVELS:
            raise ExtensionError(
                f"entrypoints.instructions.level must be one of {sorted(_INSTRUCTION_LEVELS)}"
            )
        items.append({"name": name, "path": path, "level": level})
    return items


def _validate_skills(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.skills must be a list")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.skills items must be objects")
        name = str(item.get("name") or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ExtensionError("entrypoints.skills.name contains invalid characters")
        if name in seen:
            raise ExtensionError(f"entrypoints.skills contains duplicate name: {name}")
        seen.add(name)
        path = _clean_rel_path(str(item.get("path") or ""), field="entrypoints.skills.path")
        items.append({"name": name, "path": path})
    return items


_CAPABILITY_SCOPES = {"global", "project", "session", "turn", "runtime"}
_CAPABILITY_GATES = {"internal", "external"}


def _validate_capability_release(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"timeout_s": None, "after_task": False}
    if not isinstance(raw, dict):
        raise ExtensionError("entrypoints.capabilities.release must be an object")
    timeout = raw.get("timeout_s")
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0
    ):
        raise ExtensionError("entrypoints.capabilities.release.timeout_s must be a positive integer")
    unknown = sorted(set(raw) - {"timeout_s", "after_task"})
    if unknown:
        raise ExtensionError(
            f"entrypoints.capabilities.release has unknown keys: {', '.join(unknown)}"
        )
    return {"timeout_s": timeout, "after_task": bool(raw.get("after_task"))}


def _validate_capabilities(value: Any, *, extension_id: str) -> list[dict[str, Any]]:
    """A capability is a scoped bundle of contributions the session can load at
    runtime. Delivery reuses existing channels: ``mcp`` items self-gate on the
    per-session active set via a ``contains`` predicate, ``skill`` items are
    merged into the turn's skill set at assembly. Catalog metadata (scope, gate,
    bare_allowed, release policy) drives load validation and the release sweep."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.capabilities must be a list")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.capabilities items must be objects")
        cid = str(item.get("id") or "").strip()
        if not _ID_RE.fullmatch(cid):
            raise ExtensionError("entrypoints.capabilities.id contains invalid characters")
        if cid in seen:
            raise ExtensionError(f"entrypoints.capabilities contains duplicate id: {cid}")
        seen.add(cid)
        scope = str(item.get("scope") or "").strip()
        if scope not in _CAPABILITY_SCOPES:
            raise ExtensionError(
                f"entrypoints.capabilities.scope must be one of {sorted(_CAPABILITY_SCOPES)}"
            )
        gate = str(item.get("scope_gate") or "internal").strip()
        if gate not in _CAPABILITY_GATES:
            raise ExtensionError(
                f"entrypoints.capabilities.scope_gate must be one of {sorted(_CAPABILITY_GATES)}"
            )
        mcp = (
            _validate_string_list(item.get("mcp"), field="entrypoints.capabilities.mcp")
            if item.get("mcp") is not None
            else []
        )
        skill = (
            _validate_string_list(item.get("skill"), field="entrypoints.capabilities.skill")
            if item.get("skill") is not None
            else []
        )
        items.append({
            "id": cid,
            "scope": scope,
            "bare_allowed": bool(item.get("bare_allowed")),
            "scope_gate": gate,
            "release": _validate_capability_release(item.get("release")),
            "mcp": mcp,
            "skill": skill,
        })
    return items


def _validate_team_definitions(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.team_definitions must be a list")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.team_definitions items must be objects")
        name = str(item.get("name") or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ExtensionError("entrypoints.team_definitions.name contains invalid characters")
        if name in seen:
            raise ExtensionError(f"entrypoints.team_definitions contains duplicate name: {name}")
        seen.add(name)
        path = _clean_rel_path(str(item.get("path") or ""), field="entrypoints.team_definitions.path")
        items.append({"name": name, "path": path})
    return items


def _validate_frontend_modules(value: Any, *, frontend_path: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.frontend_modules must be a list")
    if value and not frontend_path:
        raise ExtensionError("entrypoints.frontend_modules requires entrypoints.frontend")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    frontend_root = Path(frontend_path).parent
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.frontend_modules items must be objects")
        slot = str(item.get("slot") or "").strip()
        if not _ID_RE.fullmatch(slot):
            raise ExtensionError("entrypoints.frontend_modules.slot contains invalid characters")
        slot_id = str(item.get("id") or slot).strip()
        if not _ID_RE.fullmatch(slot_id):
            raise ExtensionError("entrypoints.frontend_modules.id contains invalid characters")
        key = f"{slot}:{slot_id}"
        if key in seen:
            raise ExtensionError(f"entrypoints.frontend_modules contains duplicate slot id: {key}")
        seen.add(key)
        label = str(item.get("label") or "").strip()
        if not label:
            raise ExtensionError("entrypoints.frontend_modules.label is required")
        kind = str(item.get("kind") or "module").strip()
        if kind not in _FRONTEND_MODULE_KINDS:
            raise ExtensionError(
                f"entrypoints.frontend_modules.kind must be one of {sorted(_FRONTEND_MODULE_KINDS)}"
            )
        path = _clean_rel_path(str(item.get("module") or ""), field="entrypoints.frontend_modules.module")
        rel = Path(path)
        if not rel.is_relative_to(frontend_root):
            raise ExtensionError(
                f"entrypoints.frontend_modules.module for item {index} must live under the frontend asset directory"
            )
        items.append({"slot": slot, "id": slot_id, "label": label, "kind": kind, "module": path})
    return items


# A site-relative URL the browser fetches against its own origin (the backend).
# Single leading slash only — rejects protocol-relative `//host` and absolutes.
_REL_URL_RE = re.compile(r"/[A-Za-z0-9_./~%()?=&+:-]*")
_REL_URL_TEMPLATE_RE = re.compile(r"/[A-Za-z0-9_./~%()?=&+:{}-]*")
_HOOK_ID_FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_HOOK_ICON_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_HOOK_ACTION_TYPES = {"navigate", "ensure", "module"}


def _clean_relative_url(value: Any, *, field: str) -> str:
    url = str(value or "").strip()
    if not url:
        raise ExtensionError(f"{field} is required")
    if not url.startswith("/") or url.startswith("//"):
        raise ExtensionError(f"{field} must be a site-relative URL starting with a single '/'")
    if not _REL_URL_RE.fullmatch(url):
        raise ExtensionError(f"{field} contains invalid characters")
    return url


def _extension_frontend_module_url(
    value: Any,
    *,
    field: str,
    frontend_path: str,
    extension_id: str,
) -> str:
    if not frontend_path:
        raise ExtensionError(f"{field} requires entrypoints.frontend")
    path = _clean_rel_path(str(value or ""), field=field)
    rel = Path(path)
    frontend_root = Path(frontend_path).parent
    if not rel.is_relative_to(frontend_root):
        raise ExtensionError(f"{field} must live under the frontend asset directory")
    return f"/api/extensions/{extension_id}/frontend/{path}"


def _validate_hook_action(
    value: Any,
    *,
    field: str,
    allowed: set[str],
    frontend_path: str = "",
    extension_id: str = "",
) -> dict[str, Any]:
    """A click handler for a quick_button or page.open.

    - navigate: go to a frontend route.
    - ensure: POST a backend endpoint (best-effort), then navigate to a route
      built from the response (``{id_field}`` substituted into ``path_template``).
    - module: mount a frontend module from the extension frontend asset root;
      quick buttons only (a page opens a route, not a module).
    """
    if not isinstance(value, dict):
        raise ExtensionError(f"{field} must be an object")
    action_type = str(value.get("type") or "").strip()
    if action_type not in allowed:
        raise ExtensionError(f"{field}.type must be one of: {', '.join(sorted(allowed))}")
    if action_type == "navigate":
        return {"type": "navigate", "path": _clean_relative_url(value.get("path"), field=f"{field}.path")}
    if action_type == "ensure":
        id_field = str(value.get("id_field") or "session_id").strip()
        if not _HOOK_ID_FIELD_RE.fullmatch(id_field):
            raise ExtensionError(f"{field}.id_field must be a valid identifier")
        template = str(value.get("path_template") or "").strip()
        if not template:
            raise ExtensionError(f"{field}.path_template is required")
        if not template.startswith("/") or template.startswith("//"):
            raise ExtensionError(f"{field}.path_template must start with a single '/'")
        if not _REL_URL_TEMPLATE_RE.fullmatch(template):
            raise ExtensionError(f"{field}.path_template contains invalid characters")
        return {
            "type": "ensure",
            "endpoint": _clean_relative_url(value.get("endpoint"), field=f"{field}.endpoint"),
            "path_template": template,
            "id_field": id_field,
            "include_cwd": value.get("include_cwd") is True,
        }
    return {
        "type": "module",
        "module_url": _extension_frontend_module_url(
            value.get("module_url"),
            field=f"{field}.module_url",
            frontend_path=frontend_path,
            extension_id=extension_id,
        ),
    }


def _validate_hook_icon(value: Any, *, field: str) -> str:
    icon = str(value or "").strip()
    if icon and not _HOOK_ICON_RE.fullmatch(icon):
        raise ExtensionError(f"{field} contains invalid characters")
    return icon


def _validate_quick_button(value: Any, *, frontend_path: str, extension_id: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError("entrypoints.quick_button must be an object")
    label = str(value.get("label") or "").strip()
    if not label:
        raise ExtensionError("entrypoints.quick_button.label is required")
    result: dict[str, Any] = {
        "label": label,
        "action": _validate_hook_action(
            value.get("action"),
            field="entrypoints.quick_button.action",
            allowed=_HOOK_ACTION_TYPES,
            frontend_path=frontend_path,
            extension_id=extension_id,
        ),
    }
    icon = _validate_hook_icon(value.get("icon"), field="entrypoints.quick_button.icon")
    if icon:
        result["icon"] = icon
    return result


def _validate_badge(value: Any, *, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ExtensionError(f"{field} must be an object")
    return {"endpoint": _clean_relative_url(value.get("endpoint"), field=f"{field}.endpoint")}


def _validate_page(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError("entrypoints.page must be an object")
    label = str(value.get("label") or "").strip()
    if not label:
        raise ExtensionError("entrypoints.page.label is required")
    page_id = str(value.get("id") or "main").strip()
    if not _ID_RE.fullmatch(page_id):
        raise ExtensionError("entrypoints.page.id contains invalid characters")
    result: dict[str, Any] = {
        "id": page_id,
        "label": label,
        "open": _validate_hook_action(
            value.get("open"),
            field="entrypoints.page.open",
            allowed={"navigate", "ensure"},
        ),
    }
    icon = _validate_hook_icon(value.get("icon"), field="entrypoints.page.icon")
    if icon:
        result["icon"] = icon
    badge_raw = value.get("badge")
    if badge_raw is not None:
        result["badge"] = _validate_badge(badge_raw, field="entrypoints.page.badge")
    return result


_SETTING_TYPES = {"string", "number", "boolean", "secret"}
_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_TAG_RULE_TAG_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TAG_RULE_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TAG_RULE_CLEAR_ON = {"view"}


def _validate_highlight(value: Any, *, prefix: str) -> dict[str, Any]:
    """`{"color": "#rrggbb", "alpha": 0..1}` — a transparent background tint
    applied to the tag's inner text. Fail-closed on unknown keys / bad types."""
    if not isinstance(value, dict):
        raise ExtensionError(f"{prefix} must be an object")
    unknown = set(value) - {"color", "alpha"}
    if unknown:
        raise ExtensionError(f"{prefix} has unknown keys: {', '.join(sorted(unknown))}")
    color = value.get("color")
    if not isinstance(color, str) or not _TAG_RULE_COLOR_RE.fullmatch(color):
        raise ExtensionError(f"{prefix}.color must match ^#[0-9a-fA-F]{{6}}$")
    alpha = value.get("alpha", 0.2)
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
        raise ExtensionError(f"{prefix}.alpha must be a number")
    if not (0.0 <= float(alpha) <= 1.0):
        raise ExtensionError(f"{prefix}.alpha must be in [0.0, 1.0]")
    return {"color": color, "alpha": float(alpha)}


def _validate_applied_config(value: Any, *, extension_id: str) -> dict[str, Any]:
    """Declarative, auto-reverting render rules an extension applies to
    user-visible assistant text. STRICT, fail-closed validation; rejects
    unknown keys at every level. Returns a normalized
    ``{"tag_rules": [<flat rule>]}`` dict (style flattened into the rule)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError("entrypoints.applied_config must be an object")
    unknown = set(value) - {"tag_rules"}
    if unknown:
        raise ExtensionError(
            f"entrypoints.applied_config has unknown keys: {', '.join(sorted(unknown))}"
        )
    rules_raw = value.get("tag_rules")
    if rules_raw is None:
        return {"tag_rules": []}
    if not isinstance(rules_raw, list):
        raise ExtensionError("entrypoints.applied_config.tag_rules must be a list")
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rules_raw):
        prefix = f"entrypoints.applied_config.tag_rules[{index}]"
        if not isinstance(raw, dict):
            raise ExtensionError(f"{prefix} must be an object")
        unknown = set(raw) - {"tag", "strip_wrapper", "style", "marker", "clear_on"}
        if unknown:
            raise ExtensionError(f"{prefix} has unknown keys: {', '.join(sorted(unknown))}")
        tag = raw.get("tag")
        if not isinstance(tag, str) or not _TAG_RULE_TAG_RE.fullmatch(tag):
            raise ExtensionError(f"{prefix}.tag must match ^[A-Z][A-Z0-9_]{{0,63}}$")
        if tag in seen:
            raise ExtensionError(f"{prefix}.tag is a duplicate: {tag}")
        seen.add(tag)
        rule: dict[str, Any] = {"tag": tag}

        strip_wrapper = raw.get("strip_wrapper", True)
        if not isinstance(strip_wrapper, bool):
            raise ExtensionError(f"{prefix}.strip_wrapper must be a boolean")
        rule["strip_wrapper"] = strip_wrapper

        style_raw = raw.get("style")
        if style_raw is not None:
            if not isinstance(style_raw, dict):
                raise ExtensionError(f"{prefix}.style must be an object")
            unknown = set(style_raw) - {"bold", "font_scale", "highlight"}
            if unknown:
                raise ExtensionError(f"{prefix}.style has unknown keys: {', '.join(sorted(unknown))}")
            if "bold" in style_raw:
                if not isinstance(style_raw["bold"], bool):
                    raise ExtensionError(f"{prefix}.style.bold must be a boolean")
                rule["bold"] = style_raw["bold"]
            if "font_scale" in style_raw:
                scale = style_raw["font_scale"]
                if isinstance(scale, bool) or not isinstance(scale, (int, float)):
                    raise ExtensionError(f"{prefix}.style.font_scale must be a number")
                if not (1.0 <= float(scale) <= 3.0):
                    raise ExtensionError(f"{prefix}.style.font_scale must be in [1.0, 3.0]")
                rule["font_scale"] = float(scale)
            highlight_raw = style_raw.get("highlight")
            if highlight_raw is not None:
                rule["highlight"] = _validate_highlight(highlight_raw, prefix=prefix + ".style.highlight")

        marker_raw = raw.get("marker")
        if marker_raw is not None:
            if not isinstance(marker_raw, dict):
                raise ExtensionError(f"{prefix}.marker must be an object")
            unknown = set(marker_raw) - {"color", "tooltip", "sound"}
            if unknown:
                raise ExtensionError(f"{prefix}.marker has unknown keys: {', '.join(sorted(unknown))}")
            color = marker_raw.get("color")
            if not isinstance(color, str) or not _TAG_RULE_COLOR_RE.fullmatch(color):
                raise ExtensionError(f"{prefix}.marker.color must match ^#[0-9a-fA-F]{{6}}$")
            tooltip = marker_raw.get("tooltip")
            if not isinstance(tooltip, str) or len(tooltip) > 80:
                raise ExtensionError(f"{prefix}.marker.tooltip must be a string of length <= 80")
            marker: dict[str, Any] = {"color": color, "tooltip": tooltip}
            if "sound" in marker_raw:
                if not isinstance(marker_raw["sound"], bool):
                    raise ExtensionError(f"{prefix}.marker.sound must be a boolean")
                marker["sound"] = marker_raw["sound"]
            rule["marker"] = marker

        clear_on = raw.get("clear_on")
        if clear_on is not None:
            if clear_on not in _TAG_RULE_CLEAR_ON:
                raise ExtensionError(
                    f"{prefix}.clear_on must be one of: {', '.join(sorted(_TAG_RULE_CLEAR_ON))}"
                )
            rule["clear_on"] = clear_on

        rules.append(rule)
    return {"tag_rules": rules}


def _validate_settings(value: Any) -> list[dict[str, Any]]:
    """Declarative config fields an extension surfaces in Settings.

    Stored values are user-supplied; ``secret`` types route to the OS
    keychain (never plaintext). List order is the author's display order.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.settings must be a list")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ExtensionError(f"entrypoints.settings[{index}] must be an object")
        key = str(raw.get("key") or "").strip()
        if not _SETTING_KEY_RE.fullmatch(key):
            raise ExtensionError(
                f"entrypoints.settings[{index}].key must be a lowercase snake_case identifier"
            )
        if key in seen:
            raise ExtensionError(f"entrypoints.settings contains duplicate key: {key}")
        seen.add(key)
        label = str(raw.get("label") or "").strip()
        if not label:
            raise ExtensionError(f"entrypoints.settings[{index}].label is required")
        setting_type = str(raw.get("type") or "string").strip()
        if setting_type not in _SETTING_TYPES:
            raise ExtensionError(
                f"entrypoints.settings[{index}].type must be one of: {', '.join(sorted(_SETTING_TYPES))}"
            )
        item: dict[str, Any] = {"key": key, "label": label, "type": setting_type}
        help_text = str(raw.get("help") or "").strip()
        if help_text:
            item["help"] = help_text
        if "default" in raw and raw["default"] is not None:
            item["default"] = _coerce_setting_value(raw["default"], setting_type, key, enum=raw.get("enum"))
        enum_raw = raw.get("enum")
        if enum_raw is not None:
            if setting_type in {"boolean", "secret"}:
                raise ExtensionError(f"entrypoints.settings[{index}].enum is only valid for string/number")
            if not isinstance(enum_raw, list) or not enum_raw:
                raise ExtensionError(f"entrypoints.settings[{index}].enum must be a non-empty list")
            item["enum"] = [_coerce_setting_value(v, setting_type, key) for v in enum_raw]
        items.append(item)
    return items


def _coerce_setting_value(value: Any, setting_type: str, key: str, *, enum: Any = None) -> Any:
    """Validate + coerce a setting value against its declared type. Fail closed
    on mismatch (never silently coerce garbage)."""
    if setting_type == "boolean":
        if not isinstance(value, bool):
            raise ExtensionError(f"settings.{key} default must be a boolean")
        return value
    if setting_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExtensionError(f"settings.{key} default must be a number")
        return value
    # string / secret
    if not isinstance(value, str):
        raise ExtensionError(f"settings.{key} default must be a string")
    if enum is not None and isinstance(enum, list) and value not in enum:
        raise ExtensionError(f"settings.{key} default must be one of the enum values")
    return value


def _validate_mcp_predicate(raw: Any) -> dict[str, Any]:
    """Declarative run-input gate for an MCP server (no code — safe for
    untrusted extensions). Clauses: equals/not_equals ({input_key: scalar}),
    nonempty ([input_key]). Evaluated against provider run inputs, so an
    installed extension can scope its MCP the way the old builtin predicates
    did (e.g. session-bridge: mode==native, working_mode!=search_worker)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ExtensionError("entrypoints.mcp.predicate must be an object")
    predicate: dict[str, Any] = {}
    for clause in ("equals", "not_equals", "contains"):
        sub = raw.get(clause)
        if sub is None:
            continue
        if not isinstance(sub, dict) or not all(isinstance(k, str) for k in sub):
            raise ExtensionError(
                f"entrypoints.mcp.predicate.{clause} must be an object of {{input_key: scalar}}"
            )
        predicate[clause] = {str(k): str(v) for k, v in sub.items()}
    nonempty = raw.get("nonempty")
    if nonempty is not None:
        predicate["nonempty"] = _validate_string_list(nonempty, field="entrypoints.mcp.predicate.nonempty")
    unknown = sorted(set(raw) - {"equals", "not_equals", "contains", "nonempty"})
    if unknown:
        raise ExtensionError(f"entrypoints.mcp.predicate has unknown keys: {', '.join(unknown)}")
    return predicate


def _mcp_predicate_matches(predicate: dict[str, Any], inputs: dict[str, Any]) -> bool:
    for key, expected in (predicate.get("equals") or {}).items():
        if str(inputs.get(key) or "") != expected:
            return False
    for key, forbidden in (predicate.get("not_equals") or {}).items():
        if str(inputs.get(key) or "") == forbidden:
            return False
    for key, needle in (predicate.get("contains") or {}).items():
        haystack = inputs.get(key)
        if not isinstance(haystack, (list, tuple, set)):
            return False
        if needle not in {str(member) for member in haystack}:
            return False
    for key in (predicate.get("nonempty") or []):
        if not inputs.get(key):
            return False
    return True


def _validate_mcp_entrypoints(value: Any, *, extension_id: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.mcp must be a list")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            raise ExtensionError(
                "entrypoints.mcp items must declare {name, python}; "
                f"item {index} only declared a name"
            )
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.mcp items must be objects")
        name = str(item.get("name") or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ExtensionError("entrypoints.mcp.name contains invalid characters")
        replaces_builtin = str(item.get("replaces_builtin") or "").strip()
        if replaces_builtin and replaces_builtin not in _RESERVED_MCP_SERVER_NAMES:
            raise ExtensionError("entrypoints.mcp.replaces_builtin must be a reserved MCP server name")
        if replaces_builtin not in _BUILTIN_MCP_REPLACEMENTS_BY_EXTENSION_ID.get(extension_id, frozenset()):
            if replaces_builtin:
                raise ExtensionError(
                    "entrypoints.mcp.replaces_builtin is not allowed for this extension id"
                )
        if name in _RESERVED_MCP_SERVER_NAMES:
            raise ExtensionError(f"entrypoints.mcp.name is reserved: {name}")
        python_raw = str(item.get("python") or "").strip()
        module_raw = str(item.get("module") or "").strip()
        command_raw = str(item.get("command") or "").strip()
        declared = [name for name, raw in (("python", python_raw), ("module", module_raw), ("command", command_raw)) if raw]
        if len(declared) > 1:
            raise ExtensionError("entrypoints.mcp item must declare only one of python, module, or command")
        if not declared:
            raise ExtensionError("entrypoints.mcp item must declare python, module, or command")
        python_path = ""
        if python_raw:
            python_path = _clean_rel_path(python_raw, field="entrypoints.mcp.python")
        module = _clean_optional_python_module(module_raw, field="entrypoints.mcp.module")
        command = ""
        if command_raw:
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", command_raw):
                raise ExtensionError("entrypoints.mcp.command contains invalid characters")
            command = command_raw
        args = _validate_string_list(item.get("args"), field="entrypoints.mcp.args")
        env_raw = item.get("env") or {}
        if not isinstance(env_raw, dict):
            raise ExtensionError("entrypoints.mcp.env must be an object")
        env: dict[str, str] = {}
        for key, raw_value in env_raw.items():
            key = str(key or "").strip()
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,79}", key):
                raise ExtensionError("entrypoints.mcp.env keys must be uppercase env names")
            env[key] = str(raw_value)
        items.append(
            {
                "name": name,
                "python": python_path,
                "module": module,
                "command": command,
                "args": args,
                "env": env,
                "user_facing": item.get("user_facing") is not False,
                "bare_allowed": item.get("bare_allowed") is True,
                "requires_backend_auth": item.get("requires_backend_auth") is not False,
                "replaces_builtin": replaces_builtin,
                "predicate": _validate_mcp_predicate(item.get("predicate")),
            }
        )
    return items


def _stored_mcp_entrypoints(record: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    raw_items = entrypoints.get("mcp") or []
    if not isinstance(raw_items, list):
        raise ExtensionError("stored extension entrypoints.mcp must be a list")
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, str):
            name = raw.strip()
            if not _ID_RE.fullmatch(name):
                raise ExtensionError("stored extension entrypoints.mcp.name contains invalid characters")
            items.append({"name": name})
            continue
        if not isinstance(raw, dict):
            raise ExtensionError("stored extension entrypoints.mcp items must be objects or strings")
        items.append(raw)
    return items


# Session-record fields an extension may mutate via the scoped
# /api/internal/session-field endpoint. Each maps to a tested session_manager
# setter; the extension declares a subset under permissions.mutates_session_fields.
_MUTABLE_SESSION_FIELDS = frozenset({
    "supervisor_enabled",
    "pending_supervisor_verdict",
    "clear_pending_supervisor_verdict",
    "rearranger_run",
    "rearranger_enabled",
    "current_todos",
    "current_tasks",
})
_READABLE_SESSION_FIELDS = frozenset({
    "current_todos",
    "current_tasks",
})


def _validate_permissions(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError("permissions must be an object")
    allowed = {
        "session_state",
        "spawn_runs",
        "internal_loopback",
        "filesystem",
        "network",
        "secrets",
        "provider_config",
        "backend_routes",
        "storage",
        "payments",
        "reads_session_fields",
        "mutates_session_fields",
        "managed_run_env",
        "in_process_execution",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExtensionError(f"permissions contains unknown keys: {', '.join(unknown)}")
    permissions: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, bool):
            permissions[key] = item
            continue
        if item == "optional":
            permissions[key] = "optional"
            continue
        if isinstance(item, list) and all(isinstance(part, str) and part.strip() for part in item):
            permissions[key] = [part.strip() for part in item]
            continue
        raise ExtensionError(f"permissions.{key} must be a boolean, 'optional', or string list")
    declared_fields = permissions.get("mutates_session_fields")
    if declared_fields is not None:
        bad = sorted(set(declared_fields) - _MUTABLE_SESSION_FIELDS)
        if bad:
            raise ExtensionError(
                f"permissions.mutates_session_fields has unknown fields: {', '.join(bad)}"
            )
    readable_fields = permissions.get("reads_session_fields")
    if readable_fields is not None:
        bad = sorted(set(readable_fields) - _READABLE_SESSION_FIELDS)
        if bad:
            raise ExtensionError(
                f"permissions.reads_session_fields has unknown fields: {', '.join(bad)}"
            )
    declared_env = permissions.get("managed_run_env")
    if declared_env is not None:
        bad = sorted(
            key for key in declared_env
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,79}", key)
        )
        if bad:
            raise ExtensionError(
                f"permissions.managed_run_env has invalid env keys: {', '.join(bad)}"
            )
    return permissions


def _validate_dependencies(value: Any, *, extension_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("dependencies must be a list of extension ids")
    normalized: list[str] = []
    seen: set[str] = set()
    for dep in value:
        dep = str(dep or "").strip()
        if not _ID_RE.fullmatch(dep):
            raise ExtensionError(f"dependencies entry is not a valid extension id: {dep!r}")
        if dep == extension_id:
            raise ExtensionError("dependencies must not include the extension itself")
        if dep not in seen:
            seen.add(dep)
            normalized.append(dep)
    return normalized


def _validate_hooks(value: Any, *, has_backend: bool) -> dict[str, Any]:
    """Declarative lifecycle hooks an extension subscribes to. Today:
    ``pre_turn`` — core invokes fire-and-forget before a turn runs (on
    ``lifecycle.turn_start``); ``post_turn`` — core invokes fire-and-forget
    after ``lifecycle.turn_complete``. Both receive the turn context and
    require ``entrypoints.backend`` (the hook is a backend invocation)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError("entrypoints.hooks must be an object")
    hooks: dict[str, Any] = {}
    pre_turn = value.get("pre_turn")
    if pre_turn is not None:
        if not has_backend:
            raise ExtensionError("entrypoints.hooks.pre_turn requires entrypoints.backend")
        pre_turn = str(pre_turn).strip()
        if not pre_turn.startswith("/"):
            raise ExtensionError("entrypoints.hooks.pre_turn must be a path starting with /")
        hooks["pre_turn"] = pre_turn
    post_turn = value.get("post_turn")
    if post_turn is not None:
        if not has_backend:
            raise ExtensionError("entrypoints.hooks.post_turn requires entrypoints.backend")
        post_turn = str(post_turn).strip()
        if not post_turn.startswith("/"):
            raise ExtensionError("entrypoints.hooks.post_turn must be a path starting with /")
        hooks["post_turn"] = post_turn
    session_event = value.get("session_event")
    if session_event is not None:
        if not has_backend:
            raise ExtensionError("entrypoints.hooks.session_event requires entrypoints.backend")
        session_event = str(session_event).strip()
        if not session_event.startswith("/"):
            raise ExtensionError("entrypoints.hooks.session_event must be a path starting with /")
        hooks["session_event"] = session_event
    unknown = sorted(set(value) - {"pre_turn", "post_turn", "session_event"})
    if unknown:
        raise ExtensionError(f"entrypoints.hooks has unknown keys: {', '.join(unknown)}")
    return hooks


def _validate_backend_timeouts(raw: Any) -> dict[str, float]:
    """Per-route extension-backend call timeouts (seconds). Keys are backend
    route subpaths (the path after ``/backend/``, slash-normalized) or the
    special ``default`` applied to any route without an explicit entry. Values
    are positive numbers. Fail closed: a malformed entry rejects the whole
    manifest rather than silently dropping to the 30s host default."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ExtensionError("entrypoints.backend_timeouts must be an object")
    result: dict[str, float] = {}
    for key, value in raw.items():
        route = "default" if key == "default" else str(key).strip().strip("/")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExtensionError(f"entrypoints.backend_timeouts['{key}'] must be a number")
        if value <= 0:
            raise ExtensionError(f"entrypoints.backend_timeouts['{key}'] must be a positive number")
        result[route] = float(value)
    return result


def _validate_backend_retry_on_exit(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ExtensionError("entrypoints.backend_retry_on_exit must be an array")
    result: list[str] = []
    for index, value in enumerate(raw):
        route = str(value or "").strip().strip("/")
        if not route or route == "default" or route.startswith(".") or ".." in route.split("/"):
            raise ExtensionError(
                f"entrypoints.backend_retry_on_exit[{index}] must be a backend route subpath"
            )
        result.append(route)
    return tuple(dict.fromkeys(result))


def validate_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ExtensionError("Manifest must be a JSON object")
    if raw.get("kind") != MANIFEST_KIND:
        raise ExtensionError(f"Manifest kind must be {MANIFEST_KIND!r}")
    extension_id = str(raw.get("id") or "").strip()
    if not _ID_RE.fullmatch(extension_id):
        raise ExtensionError("Manifest id must be 3-80 lowercase letters, digits, dots, underscores, or hyphens")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ExtensionError("Manifest name is required")
    version = str(raw.get("version") or "").strip()
    if not _VERSION_RE.fullmatch(version):
        raise ExtensionError("Manifest version is required and contains invalid characters")
    surfaces = [
        # Backward compat: "provider_capabilities" surface renamed to "instructions".
        "instructions" if s == "provider_capabilities" else s
        for s in _validate_string_list(raw.get("surfaces"), field="surfaces")
    ]
    unknown_surfaces = sorted(set(surfaces) - _ALLOWED_SURFACES)
    if unknown_surfaces:
        raise ExtensionError(f"surfaces contains unknown values: {', '.join(unknown_surfaces)}")
    entrypoints_raw = raw.get("entrypoints") or {}
    if not isinstance(entrypoints_raw, dict):
        raise ExtensionError("entrypoints must be an object")
    backend_entrypoint = _clean_optional_rel_path(entrypoints_raw.get("backend"), field="entrypoints.backend")
    backend_module = _clean_optional_python_module(
        entrypoints_raw.get("backend_module"),
        field="entrypoints.backend_module",
    )
    if backend_entrypoint and backend_module:
        raise ExtensionError("entrypoints must declare either backend or backend_module, not both")
    frontend_entrypoint = _clean_optional_rel_path(entrypoints_raw.get("frontend"), field="entrypoints.frontend")
    entrypoints = {
        "backend": backend_entrypoint,
        "backend_module": backend_module,
        "frontend": frontend_entrypoint,
        "mcp": _validate_mcp_entrypoints(entrypoints_raw.get("mcp"), extension_id=extension_id),
        "remote_services": _validate_remote_services(entrypoints_raw.get("remote_services")),
        "instructions": _validate_instructions(
            extension_instructions.instruction_items_from_entrypoints(entrypoints_raw)
        ),
        "skills": _validate_skills(entrypoints_raw.get("skills")),
        "capabilities": _validate_capabilities(
            entrypoints_raw.get("capabilities"), extension_id=extension_id
        ),
        "team_definitions": _validate_team_definitions(entrypoints_raw.get("team_definitions")),
        "frontend_modules": _validate_frontend_modules(
            entrypoints_raw.get("frontend_modules"),
            frontend_path=frontend_entrypoint,
        ),
        "quick_button": _validate_quick_button(
            entrypoints_raw.get("quick_button"),
            frontend_path=frontend_entrypoint,
            extension_id=extension_id,
        ),
        "page": _validate_page(entrypoints_raw.get("page")),
        "settings": _validate_settings(entrypoints_raw.get("settings")),
        "python_requirements": _validate_python_requirements(entrypoints_raw.get("python_requirements")),
        "hooks": _validate_hooks(
            entrypoints_raw.get("hooks"),
            has_backend=bool(backend_entrypoint or backend_module),
        ),
        "applied_config": _validate_applied_config(
            entrypoints_raw.get("applied_config"), extension_id=extension_id
        ),
        "backend_timeouts": _validate_backend_timeouts(entrypoints_raw.get("backend_timeouts")),
        "backend_retry_on_exit": _validate_backend_retry_on_exit(
            entrypoints_raw.get("backend_retry_on_exit")
        ),
    }
    if entrypoints["frontend"] and len(Path(entrypoints["frontend"]).parts) < 2:
        raise ExtensionError("entrypoints.frontend must live under a dedicated asset directory")
    permissions = _validate_permissions(raw.get("permissions"))
    if entrypoints["remote_services"] and permissions.get("network") is not True:
        raise ExtensionError("entrypoints.remote_services requires permissions.network=true")
    marketplace_raw = raw.get("marketplace") or {}
    if not isinstance(marketplace_raw, dict):
        raise ExtensionError("marketplace must be an object")
    marketplace = {
        "product_id": str(marketplace_raw.get("product_id") or "").strip(),
        "subscription_required": marketplace_raw.get("subscription_required") is True,
        "entitlement_url": str(marketplace_raw.get("entitlement_url") or "").strip(),
    }
    if marketplace["subscription_required"] and not marketplace["product_id"]:
        raise ExtensionError("marketplace.product_id is required when subscription_required is true")
    manifest = {
        "kind": MANIFEST_KIND,
        "id": extension_id,
        "name": name,
        "version": version,
        "description": str(raw.get("description") or "").strip(),
        "surfaces": surfaces,
        "entrypoints": entrypoints,
        "permissions": permissions,
        "dependencies": _validate_dependencies(raw.get("dependencies"), extension_id=extension_id),
        "protocol": (
            _validate_protocol(raw.get("protocol"))
            if "protocol" in raw
            else _default_protocol_for_entrypoints(entrypoints)
        ),
        "marketplace": marketplace,
    }
    _validate_protocol_coverage(manifest)
    return manifest


def _validate_repo_url(repo_url: str) -> str:
    repo_url = str(repo_url or "").strip()
    if _GIT_SCP_RE.fullmatch(repo_url):
        return repo_url
    parsed = urlparse(repo_url)
    if parsed.scheme == "file":
        _validate_private_file_repo_url(parsed)
        return repo_url
    if parsed.scheme not in {"https", "ssh"} or not parsed.netloc:
        raise ExtensionError("repo_url must be an https, ssh, or trusted private file git URL")
    if parsed.username or parsed.password:
        raise ExtensionError("repo_url must not embed credentials")
    return repo_url


def _validate_private_file_repo_url(parsed) -> None:
    if parsed.netloc not in ("", "localhost"):
        raise ExtensionError("file extension repo URLs must be local")
    path = Path(urllib.request.url2pathname(parsed.path)).resolve()
    roots = _trusted_extension_file_roots()
    if not any(path.is_relative_to(root) for root in roots):
        raise ExtensionError("file extension repo URLs must be under a trusted extension file root")


def _trusted_extension_file_roots() -> list[Path]:
    raw = str(os.environ.get("BETTER_AGENT_TRUSTED_EXTENSION_FILE_ROOTS") or "").strip()
    if not raw:
        return []
    roots = []
    for item in raw.split(os.pathsep):
        if item.strip():
            roots.append(Path(item).expanduser().resolve())
    return roots


def _required_marketplace_repo_root() -> Path | None:
    raw = str(os.environ.get("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _local_required_marketplace_repo_root() -> Path | None:
    configured = _required_marketplace_repo_root()
    if configured is not None:
        return configured
    if os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") == "1":
        return None
    return _repo_root()


def _local_private_extension_repo_root() -> Path | None:
    configured = _required_marketplace_repo_root()
    if configured is not None:
        return configured
    if os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") == "1":
        return None
    private_root = _repo_root() / "better-agent-private"
    if private_root.is_dir():
        return private_root.resolve()
    return _repo_root()


def _marketplace_base_url() -> str:
    raw = str(os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL") or _DEFAULT_MARKETPLACE_BASE_URL).strip()
    return raw.rstrip("/")


def _marketplace_public_key() -> str:
    return str(os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY") or _DEFAULT_MARKETPLACE_PUBLIC_KEY).strip()


def _required_marketplace_metadata_url(extension_id: str) -> str:
    return f"{_marketplace_base_url()}/extensions/{quote(extension_id, safe='')}/metadata"


def marketplace_metadata_url(extension_id: str) -> str:
    clean_id = str(extension_id or "").strip()
    if not _ID_RE.fullmatch(clean_id):
        raise ExtensionError("extension_id is invalid")
    return _required_marketplace_metadata_url(clean_id)


def marketplace_catalog_url(*, query: str = "", limit: int = 20) -> str:
    clean_query = str(query or "").strip()
    if clean_query and not _MARKETPLACE_QUERY_RE.fullmatch(clean_query):
        raise ExtensionError("query contains invalid characters")
    try:
        int(limit)
    except (TypeError, ValueError) as exc:
        raise ExtensionError("limit must be an integer") from exc
    return f"{_marketplace_base_url()}/extensions.json"


def search_marketplace_catalog(*, query: str = "", limit: int = 20) -> dict[str, Any]:
    data = _fetch_json(marketplace_catalog_url(query=query, limit=limit))
    clean_query = str(query or "").strip().lower()
    if clean_query and not _MARKETPLACE_QUERY_RE.fullmatch(clean_query):
        raise ExtensionError("query contains invalid characters")
    try:
        clean_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ExtensionError("limit must be an integer") from exc
    clean_limit = max(1, min(clean_limit, 50))
    if isinstance(data.get("extensions"), list):
        rows = data["extensions"]
    elif isinstance(data.get("items"), list):
        rows = data["items"]
    else:
        raise ExtensionError("marketplace catalog response must include extensions")
    filtered = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if clean_query:
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("id", "name", "description")
            ).lower()
            if clean_query not in haystack:
                continue
        filtered.append(item)
        if len(filtered) >= clean_limit:
            break
    return {"extensions": filtered}


def _scrub(text: str) -> str:
    return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", text)


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        detail = _scrub((result.stderr or result.stdout or "git command failed").strip())
        raise ExtensionError(detail)
    return result.stdout.strip()


def _verify_entitlement(manifest: dict[str, Any], entitlement_token: str) -> dict[str, Any]:
    marketplace = manifest["marketplace"]
    if not marketplace["subscription_required"]:
        return {
            "status": "not_required",
            "product_id": marketplace["product_id"],
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        }
    token = str(entitlement_token or "").strip()
    if not token:
        raise ExtensionError("entitlement_token is required for subscription extensions")
    url = str(
        marketplace.get("entitlement_url")
        or os.environ.get("BETTER_AGENT_MARKETPLACE_ENTITLEMENT_URL")
        or ""
    ).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ExtensionError("marketplace.entitlement_url must be https for subscription extensions")
    body = json.dumps(
        {
            "extension_id": manifest["id"],
            "version": manifest["version"],
            "product_id": marketplace["product_id"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ExtensionError("entitlement verification failed") from exc
    if not isinstance(payload, dict) or payload.get("active") is not True:
        raise ExtensionError("subscription entitlement is not active")
    return {
        "status": "active",
        "product_id": marketplace["product_id"],
        "token_present": True,
        "last_checked_at": _now(),
        "expires_at": str(payload.get("expires_at") or ""),
    }


def _entitlement_active(entitlement: dict[str, Any]) -> bool:
    status = entitlement.get("status")
    if status == "not_required":
        return True
    if status != "active":
        return False
    expires_at = str(entitlement.get("expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def _decode_key_or_signature(value: str, field: str) -> bytes:
    clean = str(value or "").strip()
    if not clean:
        raise ExtensionError(f"{field} is required")
    try:
        if re.fullmatch(r"[0-9a-fA-F]+", clean) and len(clean) % 2 == 0:
            return bytes.fromhex(clean)
        return base64.b64decode(clean, validate=True)
    except ValueError as exc:
        raise ExtensionError(f"{field} is not valid hex/base64") from exc


def _artifact_signed_payload(*, extension_id: str, version: str, artifact_sha256: str) -> bytes:
    return json.dumps(
        {
            "artifact_sha256": artifact_sha256,
            "extension_id": extension_id,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _verify_artifact_signature(
    *,
    extension_id: str,
    version: str,
    artifact_sha256: str,
    signature: str,
) -> None:
    # Trust ONLY the pinned built-in/env public key. A metadata-supplied key is
    # never honored: otherwise an attacker who MITMs the metadata endpoint could
    # ship a malicious artifact plus a matching key and self-validate the
    # signature, defeating the first-party (better_agent_signed) trust anchor.
    key = _marketplace_public_key()
    if not key:
        raise ExtensionError("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY is required for marketplace artifacts")
    key_bytes = _decode_key_or_signature(key, "marketplace public key")
    signature_bytes = _decode_key_or_signature(signature, "artifact signature")
    if len(key_bytes) != 32:
        raise ExtensionError("marketplace public key must be an Ed25519 public key")
    try:
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            signature_bytes,
            _artifact_signed_payload(
                extension_id=extension_id,
                version=version,
                artifact_sha256=artifact_sha256,
            ),
        )
    except InvalidSignature as exc:
        raise ExtensionError("marketplace artifact signature is invalid") from exc


def _validate_artifact_url(url: str) -> str:
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme != "https" or not parsed.netloc:
        if os.environ.get("BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS") != "1":
            raise ExtensionError("marketplace artifact URL must be https")
    if parsed.username or parsed.password:
        raise ExtensionError("marketplace artifact URL must not embed credentials")
    return clean


def _fetch_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        _validate_artifact_url(url),
        headers={
            "Accept": "application/json",
            "User-Agent": _MARKETPLACE_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read(_MAX_ARTIFACT_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ExtensionError(
            f"marketplace metadata fetch failed: HTTP {exc.code} {exc.reason} from {_scrub(url)}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ExtensionError(
            f"marketplace metadata fetch failed: {reason} for {_scrub(url)}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ExtensionError(
            f"marketplace catalog returned non-JSON (not a marketplace API?): {_scrub(url)}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExtensionError("marketplace metadata must be an object")
    return payload


def _download_artifact(url: str) -> bytes:
    req = urllib.request.Request(
        _validate_artifact_url(url),
        headers={
            "Accept": "application/gzip",
            "User-Agent": _MARKETPLACE_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content = response.read(_MAX_ARTIFACT_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExtensionError("marketplace artifact download failed") from exc
    if len(content) > _MAX_ARTIFACT_BYTES:
        raise ExtensionError("marketplace artifact is too large")
    return content


def _safe_extract_tar_gz(archive_bytes: bytes, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    archive_path = target.parent / "artifact.tar.gz"
    archive_path.write_bytes(archive_bytes)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ExtensionError("marketplace artifact contains unsafe paths")
                resolved = (target / member.name).resolve()
                if not resolved.is_relative_to(target.resolve()):
                    raise ExtensionError("marketplace artifact path escapes package root")
                if member.islnk() or member.issym():
                    raise ExtensionError("marketplace artifact must not contain links")
            archive.extractall(target)
    except tarfile.TarError as exc:
        raise ExtensionError("marketplace artifact is not a valid tar.gz") from exc
    finally:
        archive_path.unlink(missing_ok=True)


def _build_package_artifact(package_dir: Path) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gzip_file:
        with tarfile.open(fileobj=gzip_file, mode="w") as archive:
            for path in sorted(package_dir.rglob("*")):
                rel = path.relative_to(package_dir).as_posix()
                if path.is_symlink():
                    raise ExtensionError("extension package must not contain links")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ExtensionError("extension package contains unsupported filesystem entries")
                info = archive.gettarinfo(str(path), arcname=rel)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                with path.open("rb") as fh:
                    archive.addfile(info, fh)
    return buf.getvalue()


def _install_package_artifact(package_dir: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_extract_tar_gz(_build_package_artifact(package_dir), target)


def _install_from_package_dir(
    *,
    package_dir: Path,
    source: dict[str, str],
    entitlement_token: str = "",
    force_enabled: bool = False,
    persist: bool = True,
    existing_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_path = package_dir / "better-agent-extension.json"
    if not manifest_path.exists():
        raise ExtensionError("better-agent-extension.json not found at extension_path")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    _validate_declared_files(manifest, package_dir)
    entitlement = _verify_entitlement(manifest, entitlement_token)
    commit_sha = source.get("commit_sha") or hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    existing = existing_record
    if existing is None and persist:
        existing = _load()["extensions"].get(manifest["id"])
    previous_exists = existing is not None
    existing = existing or {}
    target = _install_root() / manifest["id"] / "versions" / commit_sha
    _install_package_artifact(package_dir, target)
    try:
        _install_python_requirements(target, manifest)
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    now = _now()
    record = {
        "manifest": manifest,
        "enabled": True if force_enabled or manifest["id"] in REQUIRED_EXTENSION_IDS else existing.get("enabled", True),
        "instructions_enabled": extension_instructions.normalize_state(existing),
        "permission_grants": permission_grants(existing),
        "installed_at": existing.get("installed_at") or now,
        "updated_at": now,
        "source": {
            **source,
            "install_path": str(target),
        },
        "entitlement": entitlement,
        "smoke_test": smoke_test,
        # Install IS consent: the install UI shows the declared permissions, so
        # completing the install records consent to this exact permission set.
        # An update that changes permissions produces a new fingerprint, so the
        # update (shown in the UI) re-consents.
        "consent": {
            "fingerprint": permission_consent_fingerprint({"manifest": manifest}),
            "at": now,
        },
    }
    if persist:
        data = _load()
        data["extensions"][manifest["id"]] = record
        _save(data, resurrect_extension_ids={manifest["id"]})
        if previous_exists:
            _evict_extension_backend(manifest["id"])
        extension_instructions.reconcile_blocks(record)
        extension_applied_config.reconcile(record)
        reconcile_runtime_skills()
        reconcile_native_mcp_servers()
    return record


def _evict_extension_backend(extension_id: str) -> None:
    from extension_backend_loader import evict_persistent_backend

    evict_persistent_backend(extension_id)


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_bin_dir(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def _venv_site_packages_dir(venv_dir: Path) -> Path | None:
    if sys.platform == "win32":
        candidate = venv_dir / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    lib_dir = venv_dir / "lib"
    if not lib_dir.is_dir():
        return None
    for candidate in sorted(lib_dir.glob("python*/site-packages")):
        if candidate.is_dir():
            return candidate
    return None


def _install_python_requirements(target: Path, manifest: dict[str, Any]) -> None:
    requirements = list(manifest.get("entrypoints", {}).get("python_requirements") or [])
    if not requirements:
        return
    if os.environ.get("BETTER_AGENT_SKIP_EXTENSION_DEPENDENCY_INSTALL") == "1":
        return
    venv_dir = target / ".venv"
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = _scrub((result.stderr or result.stdout or "venv creation failed").strip())
        raise ExtensionError(f"extension dependency environment creation failed: {detail}")
    python = _venv_python(venv_dir)
    result = subprocess.run(
        [str(python), "-m", "pip", "install", *requirements],
        check=False,
        capture_output=True,
        text=True,
        timeout=10 * 60,
    )
    if result.returncode != 0:
        detail = _scrub((result.stderr or result.stdout or "pip install failed").strip())
        raise ExtensionError(f"extension dependency install failed: {detail}")


def _placeholder_record(extension_id: str, *, source_type: str, error: str = "") -> dict[str, Any]:
    now = _now()
    required = extension_id in REQUIRED_EXTENSION_IDS
    name = _PRIVATE_EXTENSION_NAMES.get(extension_id, extension_id)
    extension_path = _PRIVATE_EXTENSION_PATHS.get(extension_id) or _PUBLIC_EXTENSION_PATHS.get(extension_id, "")
    return {
        "manifest": {
            "kind": MANIFEST_KIND,
            "id": extension_id,
            "name": name,
            "version": "unavailable",
            "description": f"{name} extension package is unavailable.",
            "surfaces": [],
            "entrypoints": {
                "backend": "",
                "frontend": "",
                "mcp": [],
                "instructions": [],
            },
            "permissions": {},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        },
        "enabled": required,
        "installed_at": now,
        "updated_at": now,
        "source": {
            "type": source_type,
            "repo_url": "",
            "extension_path": extension_path,
            "ref": "",
            "commit_sha": "unavailable",
            "install_path": "",
            "error": error,
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }


def _purge_obsolete_extension_records(data: dict[str, Any]) -> bool:
    changed = False
    extensions = data["extensions"]
    for obsolete_id in _OBSOLETE_EXTENSION_IDS:
        if obsolete_id in extensions:
            extensions.pop(obsolete_id, None)
            changed = True
        # Also remove the on-disk installed package so
        # `_rehydrate_installed_extension_records` cannot resurrect the
        # retired id on the next load (it is no longer in managed paths).
        pkg_dir = _install_root() / obsolete_id
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)
            changed = True
    return changed


def _rehydrate_installed_extension_records(data: dict[str, Any]) -> bool:
    root = _install_root()
    if not root.is_dir():
        return False
    managed_ids = {*_PUBLIC_EXTENSION_PATHS, *_PRIVATE_EXTENSION_PATHS}
    deleted = set((data.get("deleted_extensions") or {}).keys())
    changed = False
    for extension_dir in sorted(root.iterdir()):
        extension_id = extension_dir.name
        if extension_id in data["extensions"] or extension_id in deleted:
            continue
        if extension_id in managed_ids and _managed_extension_package_exists(extension_id):
            continue
        versions_dir = extension_dir / "versions"
        if not versions_dir.is_dir():
            continue
        versions = sorted(
            (path for path in versions_dir.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for version_dir in versions:
            manifest_path = version_dir / "better-agent-extension.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if manifest["id"] != extension_id:
                continue
            marketplace = manifest.get("marketplace") or {}
            subscription_required = marketplace.get("subscription_required") is True
            data["extensions"][extension_id] = {
                "manifest": manifest,
                "enabled": not subscription_required,
                "installed_at": _now(),
                "updated_at": _now(),
                "instructions_enabled": extension_instructions.normalize_state({}),
                "permission_grants": {},
                "source": {
                    "type": "artifact",
                    "repo_url": "",
                    "extension_path": "",
                    "ref": "",
                    "commit_sha": version_dir.name,
                    "artifact_sha256": version_dir.name if re.fullmatch(r"[0-9a-f]{64}", version_dir.name) else "",
                    "artifact_url": "",
                    "metadata_url": "",
                    "install_path": str(version_dir),
                },
                "entitlement": {
                    "status": "missing" if subscription_required else "not_required",
                    "product_id": marketplace.get("product_id", ""),
                    "token_present": False,
                    "last_checked_at": "",
                    "expires_at": "",
                },
            }
            changed = True
            break
    return changed


def _managed_extension_package_exists(extension_id: str) -> bool:
    extension_path = _PRIVATE_EXTENSION_PATHS.get(extension_id) or _PUBLIC_EXTENSION_PATHS.get(extension_id)
    if not extension_path:
        return False
    roots: list[Path] = []
    configured = _required_marketplace_repo_root()
    if configured is not None:
        roots.append(configured)
    elif os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") != "1":
        roots.append(_repo_root())
        private_root = _repo_root() / "better-agent-private"
        if private_root.is_dir():
            roots.append(private_root.resolve())
    return any((root / extension_path).exists() for root in roots)


def _private_extension_commit_sha() -> str:
    root = _local_private_extension_repo_root()
    if root is None:
        return "local"
    if not (root / ".git").exists():
        return "local"
    try:
        return _git(["rev-parse", "HEAD"], cwd=root)
    except ExtensionError:
        return "local"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _hash_public_package(package_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(package_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _install_public_package_snapshot(
    extension_id: str,
    package_dir: Path,
    package_sha: str,
) -> dict[str, Any]:
    manifest_path = package_dir / "better-agent-extension.json"
    if not manifest_path.exists():
        raise ExtensionError("better-agent-extension.json not found at public extension path")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest["id"] != extension_id:
        raise ExtensionError("Public extension manifest id does not match install spec")
    _validate_declared_files(manifest, package_dir)
    target = _install_root() / extension_id / "versions" / package_sha
    _install_package_artifact(package_dir, target)
    try:
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    now = _now()
    return {
        "manifest": manifest,
        "enabled": True,
        "installed_at": now,
        "updated_at": now,
        "source": {
            "type": "better_agent_bundled",
            "repo_url": "",
            "extension_path": _PUBLIC_EXTENSION_PATHS[extension_id],
            "ref": "",
            "commit_sha": package_sha,
            "install_path": str(target),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": manifest["marketplace"]["product_id"],
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
        "smoke_test": smoke_test,
    }


def _install_private_package_snapshot(
    extension_id: str,
    package_dir: Path,
    *,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    manifest_path = package_dir / "better-agent-extension.json"
    if not manifest_path.exists():
        raise ExtensionError("better-agent-extension.json not found at required extension path")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest["id"] != extension_id:
        raise ExtensionError("Private extension manifest id does not match install spec")
    _validate_declared_files(manifest, package_dir)
    commit_sha = commit_sha or _private_extension_commit_sha()
    target = _install_root() / extension_id / "versions" / commit_sha
    _install_package_artifact(package_dir, target)
    try:
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    now = _now()
    repo_root = _local_private_extension_repo_root()
    mapped_path = _PRIVATE_EXTENSION_PATHS.get(extension_id)
    try:
        public_root = _repo_root().resolve()
        resolved_package_dir = package_dir.resolve()
        if resolved_package_dir.is_relative_to(public_root):
            repo_root = public_root
            mapped_path = str(resolved_package_dir.relative_to(public_root))
    except OSError:
        pass
    if mapped_path is None and repo_root is not None:
        try:
            mapped_path = str(package_dir.resolve().relative_to(repo_root))
        except ValueError:
            mapped_path = f"extensions/{package_dir.name}"
    elif mapped_path is None:
        mapped_path = f"extensions/{package_dir.name}"
    return {
        "manifest": manifest,
        "enabled": True,
        "installed_at": now,
        "updated_at": now,
        "source": {
            "type": "better_agent_local",
            "repo_url": str(repo_root or ""),
            "extension_path": mapped_path,
            "ref": "",
            "commit_sha": commit_sha,
            "install_path": str(target),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": manifest["marketplace"]["product_id"],
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
        "smoke_test": smoke_test,
    }


def _install_required_marketplace_from_ofekdev(extension_id: str) -> dict[str, Any]:
    metadata = _fetch_json(_required_marketplace_metadata_url(extension_id))
    record = install_from_artifact(
        artifact_url=str(metadata.get("artifact_url") or ""),
        artifact_sha256=str(metadata.get("artifact_sha256") or ""),
        artifact_signature=str(metadata.get("signature") or ""),
        entitlement_token="",
        expected_extension_id=extension_id,
        expected_version=str(metadata.get("version") or ""),
        source_type="better_agent_signed",
        persist=False,
    )
    return record


def _required_artifact_update_needed(extension_id: str, record: dict[str, Any]) -> bool:
    if extension_id in _required_artifact_update_checked:
        return False
    _required_artifact_update_checked.add(extension_id)
    source = record.get("source") or {}
    if source.get("type") != "better_agent_signed":
        return False
    installed_sha = str(source.get("artifact_sha256") or source.get("commit_sha") or "").strip().lower()
    try:
        metadata = _fetch_json(_required_marketplace_metadata_url(extension_id))
    except ExtensionError:
        return False
    published_sha = str(metadata.get("artifact_sha256") or "").strip().lower()
    return bool(published_sha and published_sha != installed_sha)


def _ensure_public_extensions(data: dict[str, Any]) -> bool:
    changed = False
    # Mirror _ensure_private_extensions: resolve the bundled-extensions root
    # via the env-aware _local_required_marketplace_repo_root() so non-default
    # homes (e.g. TestApe's dedicated home, which points this at
    # better-agent-private via BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH)
    # refresh public extensions from source. With no env this falls back to
    # _repo_root(), so the default home is unchanged.
    default_repo_root = None if os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") == "1" else _repo_root()
    configured_repo_root = _required_marketplace_repo_root()
    if configured_repo_root is None and default_repo_root is None:
        return False
    deleted = set((data.get("deleted_extensions") or {}).keys())
    for extension_id, extension_path in _PUBLIC_EXTENSION_PATHS.items():
        if extension_id in deleted:
            continue
        repo_root = configured_repo_root
        if repo_root is None or not (repo_root / extension_path).exists():
            repo_root = default_repo_root
        if repo_root is None:
            continue
        package_dir = (repo_root / extension_path).resolve()
        if not package_dir.is_relative_to(repo_root):
            raise ExtensionError("Public extension path escapes repository root")
        if not package_dir.exists():
            continue
        record = data["extensions"].get(extension_id)
        if record and record.get("source", {}).get("type") not in {"better_agent_bundled", "private_placeholder", ""}:
            continue
        package_sha = _hash_public_package(package_dir)
        source = record.get("source") if record else {}
        install_path_text = str(source.get("install_path") or "")
        if (
            record
            and source.get("type") == "better_agent_bundled"
            and source.get("commit_sha") == package_sha
            and install_path_text
            and Path(install_path_text).exists()
        ):
            continue
        install_error = False
        try:
            installed = _install_public_package_snapshot(extension_id, package_dir, package_sha)
        except ExtensionError as exc:
            install_error = True
            installed = _placeholder_record(
                extension_id,
                source_type="better_agent_bundled",
                error=str(exc),
            )
            installed["source"]["extension_path"] = _PUBLIC_EXTENSION_PATHS[extension_id]
            installed["source"]["commit_sha"] = package_sha
            installed["enabled"] = False
        existing = record or {}
        installed["enabled"] = False if install_error else bool(existing.get("enabled", True))
        installed["installed_at"] = existing.get("installed_at") or installed["installed_at"]
        installed["instructions_enabled"] = extension_instructions.normalize_state(existing)
        data["extensions"][extension_id] = installed
        changed = True
    return changed


def _discover_private_extensions(repo_root: Path | None) -> dict[str, str]:
    """Generic directory scan: discover private extensions by manifest, not by
    hardcoded id. Returns {extension_id: "extensions/<dir>"} for every
    better-agent-extension.json under <repo_root>/extensions/*. New private
    extensions are picked up here without a public-code entry — the public core
    never has to know their ids ahead of time.
    """
    discovered: dict[str, str] = {}
    if repo_root is None:
        return discovered
    extensions_root = repo_root / "extensions"
    if not extensions_root.is_dir():
        return discovered
    for entry in sorted(extensions_root.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "better-agent-extension.json"
        if not manifest_path.is_file():
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ext_id = str(raw.get("id") or "").strip()
        if ext_id:
            discovered[ext_id] = f"extensions/{entry.name}"
    return discovered


def _ensure_private_extensions(data: dict[str, Any]) -> bool:
    changed = False
    repo_root = _local_private_extension_repo_root()
    deleted = set((data.get("deleted_extensions") or {}).keys())
    private_commit_sha: str | None = None

    def current_private_commit_sha() -> str:
        nonlocal private_commit_sha
        if private_commit_sha is None:
            private_commit_sha = _private_extension_commit_sha()
        return private_commit_sha

    # Hardcoded map (preserves special-case entries like marketplace) augmented
    # by a generic manifest scan, so new private extensions load without a
    # public-code id entry.
    paths = dict(_PRIVATE_EXTENSION_PATHS)
    for ext_id, ext_path in _discover_private_extensions(repo_root).items():
        paths.setdefault(ext_id, ext_path)
    for extension_id, extension_path in paths.items():
        if extension_id in deleted and extension_id not in REQUIRED_EXTENSION_IDS:
            continue
        record = data["extensions"].get(extension_id)
        package_repo_root = repo_root
        package_dir = (package_repo_root / extension_path).resolve() if package_repo_root is not None else None
        if (
            record
            and extension_id not in REQUIRED_EXTENSION_IDS
            and (record.get("source") or {}).get("type") not in {"private_placeholder", ""}
        ):
            source = record.get("source") or {}
            # Re-snapshot a local-source private extension when its repo
            # advanced, so manifest/code edits in the local private repo take
            # effect on the next store reconcile without a manual reinstall.
            # Keyed on the repo HEAD commit recorded at install time. Fail-open:
            # a failed re-snapshot leaves the working install untouched.
            if (
                source.get("type") == "better_agent_local"
                and package_dir is not None
                and package_dir.exists()
                and source.get("commit_sha") != current_private_commit_sha()
            ):
                try:
                    refreshed = _install_private_package_snapshot(
                        extension_id,
                        package_dir,
                        commit_sha=current_private_commit_sha(),
                    )
                except ExtensionError:
                    continue
                refreshed["enabled"] = record.get("enabled", True)
                refreshed["installed_at"] = record.get("installed_at") or refreshed["installed_at"]
                refreshed["instructions_enabled"] = extension_instructions.normalize_state(record)
                data["extensions"][extension_id] = refreshed
                # The persistent backend subprocess was spawned with the old
                # env baked in at start (permissions, minted internal token), so
                # a manifest change that affects it stays stale until the proc
                # is recycled. Evict so the next request spawns a fresh proc.
                try:
                    from extension_backend_loader import evict_persistent_backend

                    evict_persistent_backend(extension_id)
                except Exception:
                    pass
                changed = True
            continue
        if record and (record.get("source") or {}).get("type") == "better_agent_signed":
            if _required_artifact_update_needed(extension_id, record):
                try:
                    updated = _install_required_marketplace_from_ofekdev(extension_id)
                except ExtensionError as exc:
                    source = record.get("source") or {}
                    source["error"] = str(exc)
                    record["source"] = source
                    changed = True
                else:
                    updated["enabled"] = True
                    updated["installed_at"] = record.get("installed_at") or updated["installed_at"]
                    updated["instructions_enabled"] = extension_instructions.normalize_state(record)
                    data["extensions"][extension_id] = updated
                    changed = True
                    continue
            if record.get("enabled") is not True:
                record["enabled"] = True
                record["updated_at"] = _now()
                changed = True
            continue
        if (
            extension_id == MARKETPLACE_EXTENSION_ID
            and _required_marketplace_repo_root() is None
            and os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") != "1"
        ):
            package_repo_root = _repo_root()
            package_dir = (package_repo_root / extension_path).resolve()
        if package_repo_root is None or package_dir is None or not package_dir.exists():
            if extension_id not in REQUIRED_EXTENSION_IDS:
                continue
            placeholder_error = ""
            if record and record.get("source", {}).get("type") not in {"private_placeholder", ""}:
                if _required_artifact_update_needed(extension_id, record):
                    try:
                        updated = _install_required_marketplace_from_ofekdev(extension_id)
                    except ExtensionError as exc:
                        source = record.get("source") or {}
                        source["error"] = str(exc)
                        record["source"] = source
                        changed = True
                    else:
                        updated["enabled"] = True
                        updated["installed_at"] = record.get("installed_at") or updated["installed_at"]
                        updated["instructions_enabled"] = extension_instructions.normalize_state(record)
                        data["extensions"][extension_id] = updated
                        changed = True
                        continue
                if record.get("enabled") is not True:
                    record["enabled"] = True
                    record["updated_at"] = _now()
                    changed = True
                continue
            try:
                data["extensions"][extension_id] = _install_required_marketplace_from_ofekdev(extension_id)
                changed = True
                continue
            except ExtensionError as exc:
                placeholder_error = str(exc)
            if record is None:
                data["extensions"][extension_id] = _placeholder_record(
                    extension_id,
                    source_type="private_placeholder",
                    error=placeholder_error,
                )
                changed = True
            else:
                source = record.get("source") or {}
                if source.get("error") != placeholder_error:
                    source["error"] = placeholder_error
                    record["source"] = source
                    changed = True
                if record.get("enabled") is not True:
                    record["enabled"] = True
                    record["updated_at"] = _now()
                    changed = True
            continue
        if not package_dir.is_relative_to(package_repo_root):
            raise ExtensionError("Private extension path escapes configured repo root")
        if not package_dir.exists():
            if record is None:
                data["extensions"][extension_id] = _placeholder_record(
                    extension_id,
                    source_type="private_placeholder",
                    error="private extension package not found",
                )
                changed = True
            continue
        commit_sha = current_private_commit_sha()
        source = record.get("source") if record else {}
        install_path_text = str(source.get("install_path") or "")
        if (
            record
            and source.get("type") == "better_agent_local"
            and source.get("commit_sha") == commit_sha
            and install_path_text
            and Path(install_path_text).exists()
            and not source.get("error")
            and _record_has_required_runtime_paths(record)
        ):
            continue
        install_error = False
        try:
            installed = _install_private_package_snapshot(
                extension_id,
                package_dir,
                commit_sha=commit_sha,
            )
        except (ExtensionError, OSError, subprocess.SubprocessError) as exc:
            # A broken discovered extension must not crash reconciliation — record
            # a placeholder so the store stays usable (mirrors the public path).
            # Widened beyond ExtensionError so a missing python binary, permission
            # error, or smoke-test subprocess failure is contained too.
            install_error = True
            installed = _placeholder_record(
                extension_id, source_type="private_placeholder", error=str(exc)
            )
            installed["source"]["extension_path"] = extension_path
        existing = record or {}
        if extension_id not in REQUIRED_EXTENSION_IDS:
            installed["enabled"] = False if install_error else bool(existing.get("enabled", True))
        installed["installed_at"] = existing.get("installed_at") or installed["installed_at"]
        installed["instructions_enabled"] = extension_instructions.normalize_state(existing)
        data["extensions"][extension_id] = installed
        changed = True
    return changed


def is_builtin_feature_enabled(extension_id: str) -> bool:
    data = _load()
    record = data["extensions"].get(extension_id)
    if not record:
        return False
    return _record_active(record)


def is_builtin_feature_enabled_cached(extension_id: str | None) -> bool:
    if not extension_id:
        return False
    fingerprint = store_fingerprint()
    with _BUILTIN_FEATURE_CACHE_LOCK:
        cached = _BUILTIN_FEATURE_CACHE.get(extension_id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    enabled = is_builtin_feature_enabled(extension_id)
    with _BUILTIN_FEATURE_CACHE_LOCK:
        _BUILTIN_FEATURE_CACHE[extension_id] = (fingerprint, enabled)
    return enabled


def is_extension_runtime_ready(extension_id: str) -> bool:
    record = get_extension(extension_id)
    if not record or not _record_active(record):
        return False
    return _record_runtime_ready(record)


def runtime_not_ready_reason(extension_id: str) -> str | None:
    """Classify why an extension is not runtime-ready.

    Returns None when ready, else one of: "not_installed", "disabled",
    "backend_not_ready", "needs_llm_provider". Single source of truth for
    distinguishing a genuinely-uninstalled extension from one that is installed
    but missing an internal-LLM provider assignment.
    """
    record = get_extension(extension_id)
    if not record:
        return "not_installed"
    if not _record_active(record):
        return "disabled"
    if not _record_backend_surface_ready(record):
        return "backend_not_ready"
    if _record_runtime_ready(record):
        return None
    return "needs_llm_provider"


def runtime_not_ready_message(extension_id: str) -> str | None:
    """User-facing message for why an extension is not runtime-ready.

    Returns None when ready. Uses the extension's display name so the message
    is accurate across surfaces.
    """
    reason = runtime_not_ready_reason(extension_id)
    if reason is None:
        return None
    record = get_extension(extension_id)
    name = str(((record or {}).get("manifest") or {}).get("name") or "").strip() or "Extension"
    if reason == "not_installed":
        return f"{name} is not installed"
    if reason == "disabled":
        return f"{name} is disabled"
    if reason == "needs_llm_provider":
        return f"{name} needs an LLM provider configured for session search"
    return f"{name} is not ready"


def _record_active(record: dict[str, Any]) -> bool:
    return record.get("enabled") is True and _entitlement_active(record.get("entitlement") or {})


def private_local_runtime_mode() -> str:
    raw = str(os.environ.get(_PRIVATE_LOCAL_RUNTIME_MODE_ENV) or _PRIVATE_LOCAL_RUNTIME_SOURCE).strip().lower()
    if raw in {"source", "direct", "dev"}:
        return _PRIVATE_LOCAL_RUNTIME_SOURCE
    if raw in {"packaged", "package", "snapshot"}:
        return _PRIVATE_LOCAL_RUNTIME_PACKAGED
    return _PRIVATE_LOCAL_RUNTIME_PACKAGED


def _private_local_source_root(source: dict[str, Any]) -> Path | None:
    extension_path = str(source.get("extension_path") or "").strip()
    if not extension_path:
        return None
    repo_text = str(source.get("repo_url") or "").strip()
    repo_root = Path(repo_text).expanduser() if repo_text else _local_private_extension_repo_root()
    if repo_root is None:
        return None
    try:
        repo_resolved = repo_root.resolve()
        package_dir = (repo_resolved / extension_path).resolve()
        if not package_dir.is_relative_to(repo_resolved):
            return None
    except OSError:
        return None
    return package_dir if package_dir.is_dir() else None


def runtime_package_root_for_record(record: dict[str, Any]) -> Path | None:
    source = record.get("source") or {}
    if source.get("type") == "better_agent_local" and private_local_runtime_mode() == _PRIVATE_LOCAL_RUNTIME_SOURCE:
        source_root = _private_local_source_root(source)
        if source_root is not None:
            return source_root
    install_root = Path(str(source.get("install_path") or "")).expanduser()
    if not install_root.is_dir():
        return None
    try:
        return install_root.resolve()
    except OSError:
        return None


def runtime_package_root(extension_id: str) -> Path | None:
    """Resolve an extension id to its runtime package root.

    Thin convenience wrapper around :func:`runtime_package_root_for_record`
    so callers that hold only an extension id (e.g. the startup package
    loader and the assistant UI) can resolve it without each repeating the
    record lookup. Returns ``None`` if the extension is unknown or its
    package is unavailable.
    """
    record = get_extension(extension_id)
    if not record:
        return None
    return runtime_package_root_for_record(record)


def _record_runtime_ready(record: dict[str, Any]) -> bool:
    if not _record_backend_surface_ready(record):
        return False
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    if extension_id in {BUILTIN_TODOS_EXTENSION_ID, MARKETPLACE_EXTENSION_ID}:
        return True
    task_keys = _BUILTIN_INTERNAL_LLM_TASKS.get(extension_id, ())
    if task_keys:
        return all(_internal_llm_task_ready(task_key) for task_key in task_keys)
    if not _requires_internal_llm_defaults(effective_permissions(record)):
        return True
    return _internal_llm_task_ready("default_session")


def _record_backend_surface_ready(record: dict[str, Any]) -> bool:
    if (record.get("source") or {}).get("error"):
        return False
    if not _record_has_required_runtime_paths(record):
        return False
    return _record_smoke_test_current(record)


def _record_has_required_runtime_paths(record: dict[str, Any]) -> bool:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    required = _BUILTIN_RUNTIME_REQUIRED_PATHS.get(extension_id, ())
    if not required:
        return True
    install_root = Path(str((record.get("source") or {}).get("install_path") or "")).expanduser()
    if not install_root.is_dir():
        return False
    try:
        root = install_root.resolve()
    except OSError:
        return False
    for rel in required:
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            return False
        if not path.exists():
            return False
    return True


def _requires_internal_llm_defaults(effective: dict[str, bool]) -> bool:
    return bool(effective.get("spawn_runs"))


def _internal_llm_task_ready(task_key: str) -> bool:
    """An internal LLM task is ready when it resolves to a concrete
    provider + model. Unset fields inherit the default provider (the
    Internal LLM settings contract: 'Inherit falls back to the default
    provider, so the unconfigured state is never a hardcode'), so this
    mirrors the consumer's resolution (config_store.resolve_internal_llm)
    rather than demanding an explicit per-task pin."""
    try:
        import config_store
    except Exception:
        return False
    try:
        resolved = config_store.resolve_internal_llm(task_key)
        provider_id = str(resolved.get("provider_id") or "").strip()
        model = str(resolved.get("model") or "").strip()
        return bool(provider_id) and bool(model)
    except Exception:
        return False


def builtin_feature_summary() -> dict[str, bool]:
    return {
        extension_id: is_builtin_feature_enabled(extension_id)
        for extension_id in {**_PUBLIC_EXTENSION_PATHS, **_PRIVATE_EXTENSION_PATHS}
        if extension_id != MARKETPLACE_EXTENSION_ID
    }


def _validate_declared_files(manifest: dict[str, Any], package_dir: Path) -> None:
    root = package_dir.resolve()
    for field in ("backend", "frontend"):
        declared = manifest["entrypoints"].get(field)
        if not declared:
            continue
        path = (package_dir / declared).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError(f"{field} entrypoint path escapes extension package")
        if not path.exists() or not path.is_file():
            raise ExtensionError(f"{field} entrypoint file not found: {declared}")
    for item in manifest["entrypoints"]["mcp"]:
        if not item.get("python"):
            continue  # module/command-based MCP server — no in-package file to validate
        path = (package_dir / item["python"]).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError("MCP entrypoint path escapes extension package")
        if not path.exists() or not path.is_file():
            raise ExtensionError(f"MCP entrypoint file not found: {item['python']}")
    for item in manifest["entrypoints"]["instructions"]:
        path = (package_dir / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError("instruction path escapes extension package")
        if not path.exists() or not path.is_file():
            raise ExtensionError(f"instruction file not found: {item['path']}")
    for item in manifest["entrypoints"]["skills"]:
        path = (package_dir / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError("skill path escapes extension package")
        if not path.is_dir():
            raise ExtensionError(f"skill directory not found: {item['path']}")
        if not (path / "SKILL.md").is_file():
            raise ExtensionError(f"skill SKILL.md not found: {item['path']}/SKILL.md")
    for item in manifest["entrypoints"]["team_definitions"]:
        path = (package_dir / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError("team definition path escapes extension package")
        if not path.exists() or not path.is_file():
            raise ExtensionError(f"team definition file not found: {item['path']}")
    for item in manifest["entrypoints"]["frontend_modules"]:
        path = (package_dir / item["module"]).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError("frontend module path escapes extension package")
        if not path.exists() or not path.is_file():
            raise ExtensionError(f"frontend module file not found: {item['module']}")

def _require_smoke_path(package_dir: Path, rel_path: str) -> None:
    root = package_dir.resolve()
    path = (package_dir / rel_path).resolve()
    if not path.is_relative_to(root):
        raise ExtensionError("protocol.smoke_test.required_paths escapes extension package")
    if not path.exists():
        raise ExtensionError(f"protocol.smoke_test.required_paths not found: {rel_path}")


def _smoke_python(package_dir: Path) -> Path:
    venv_python = _venv_python(package_dir / ".venv")
    if venv_python.is_file():
        return venv_python
    return Path(sys.executable)


def _smoke_static_modules(entrypoints: dict[str, Any]) -> dict[str, str]:
    """Modules that should be syntax-checked, not imported, during smoke.

    For file-path MCP entrypoints (``python: mcp/server.py``), compile the exact
    declared file. Import resolution for a local ``mcp/`` namespace is otherwise
    shadowed by the installed third-party ``mcp`` package, so ``find_spec`` can
    either compile the wrong file or fail for modules such as
    ``mcp.worker_server``.
    """
    modules = {module: "" for module in _required_smoke_python_modules(entrypoints)}
    for item in entrypoints.get("mcp") or []:
        python_path = item.get("python")
        if python_path:
            modules[_python_path_to_module(python_path)] = python_path
    return modules


# OS/interpreter-essential env vars the smoke subprocess needs to even run.
# The smoke env is otherwise kept minimal (no host app secrets), but on
# Windows winsock's WSAStartup loads its service-provider DLLs from
# %SystemRoot%\System32 and fails with OSError [WinError 10106] when
# SystemRoot is absent — which crashes `import mcp` (asyncio/anyio create a
# socket / proactor loop). Forward the platform basics so importability, not
# the host's networking config, is what's being tested.
_SMOKE_OS_ENV_KEYS = (
    "SystemRoot",
    "SYSTEMROOT",
    "SystemDrive",
    "windir",
    "TEMP",
    "TMP",
    "PATHEXT",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "LOCALAPPDATA",
    "APPDATA",
)


def _smoke_subprocess_env(python_path_parts: list[str]) -> dict[str, str]:
    """Minimal env for the smoke subprocess plus the OS-essential vars a
    Python interpreter (and Windows winsock) needs to start and import."""
    env = {
        "PYTHONPATH": os.pathsep.join(python_path_parts),
        "PATH": os.environ.get("PATH", ""),
    }
    for key in _SMOKE_OS_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env.setdefault(key, value)
    return env


def _run_python_module_smoke(
    package_dir: Path,
    modules: list[str],
    *,
    static_modules: dict[str, str] | set[str] | None = None,
) -> None:
    if not modules:
        return
    sdk_root = _repo_root() / "sdk"
    python_path_parts = [str(package_dir)]
    if sdk_root.is_dir():
        python_path_parts.append(str(sdk_root))
    existing_python_path = os.environ.get("PYTHONPATH")
    if existing_python_path:
        python_path_parts.append(existing_python_path)
    if isinstance(static_modules, dict):
        static_payload = dict(static_modules)
    else:
        static_payload = {module: "" for module in (static_modules or set())}
    code = (
        "import importlib, importlib.util, json, py_compile, sys\n"
        "from pathlib import Path\n"
        "static = json.loads(sys.argv[2])\n"
        "root = Path(sys.argv[3]).resolve()\n"
        "for module in json.loads(sys.argv[1]):\n"
        "    if module in static and static[module]:\n"
        "        path = (root / static[module]).resolve()\n"
        "        if not path.is_relative_to(root):\n"
        "            raise RuntimeError(f'smoke path escapes package: {static[module]}')\n"
        "        py_compile.compile(str(path), doraise=True)\n"
        "        continue\n"
        "    spec = importlib.util.find_spec(module)\n"
        "    if spec is None:\n"
        "        raise ModuleNotFoundError(module)\n"
        "    if module in static:\n"
        "        origin = getattr(spec, 'origin', '') or ''\n"
        "        if origin and origin not in {'built-in', 'namespace'}:\n"
        "            py_compile.compile(origin, doraise=True)\n"
        "        continue\n"
        "    importlib.import_module(module)\n"
    )
    result = subprocess.run(
        [
            str(_smoke_python(package_dir)),
            "-c",
            code,
            json.dumps(modules),
            json.dumps(static_payload, sort_keys=True),
            str(package_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=_smoke_subprocess_env(python_path_parts),
    )
    if result.returncode != 0:
        detail = _scrub((result.stderr or result.stdout or "module import failed").strip())
        raise ExtensionError(f"protocol.smoke_test.python_modules failed: {detail}")


def _run_extension_smoke_test(manifest: dict[str, Any], package_dir: Path) -> dict[str, Any]:
    protocol = _validate_protocol(manifest.get("protocol"))
    _validate_protocol_coverage({**manifest, "protocol": protocol})
    smoke = protocol["smoke_test"]
    required_paths = list(smoke.get("required_paths") or [])
    python_modules = list(smoke.get("python_modules") or [])
    for rel_path in required_paths:
        _require_smoke_path(package_dir, rel_path)
    _run_python_module_smoke(
        package_dir,
        python_modules,
        static_modules=_smoke_static_modules(manifest.get("entrypoints") or {}),
    )
    return {
        "status": "passed",
        "checked_at": _now(),
        "protocol_version": protocol.get("version", _EXTENSION_PROTOCOL_VERSION),
        "required_paths": required_paths,
        "python_modules": python_modules,
    }


def _record_smoke_test_passes(record: dict[str, Any]) -> bool:
    install_path = Path(str((record.get("source") or {}).get("install_path") or "")).expanduser()
    if not install_path.is_dir():
        return False
    try:
        manifest = record.get("manifest") or {}
        protocol = _validate_protocol(manifest.get("protocol"))
        _validate_protocol_coverage({**manifest, "protocol": protocol})
        smoke = protocol["smoke_test"]
        required_paths = list(smoke.get("required_paths") or [])
        python_modules = list(smoke.get("python_modules") or [])
        for rel_path in required_paths:
            _require_smoke_path(install_path, rel_path)
        stored = record.get("smoke_test") if isinstance(record.get("smoke_test"), dict) else {}
        if (
            stored.get("status") == "passed"
            and stored.get("protocol_version") == protocol.get("version", _EXTENSION_PROTOCOL_VERSION)
            and list(stored.get("required_paths") or []) == required_paths
            and list(stored.get("python_modules") or []) == python_modules
        ):
            return True
        _run_python_module_smoke(
            install_path,
            python_modules,
            static_modules=_smoke_static_modules(manifest.get("entrypoints") or {}),
        )
        return True
    except ExtensionError:
        return False


def _record_smoke_test_current(record: dict[str, Any]) -> bool:
    smoke_result = record.get("smoke_test") or {}
    manifest = record.get("manifest") or {}
    if not smoke_result and "protocol" not in manifest:
        return True
    if not smoke_result:
        return _record_smoke_test_passes(record)
    if smoke_result.get("status") != "passed":
        return False
    protocol = _validate_protocol(manifest.get("protocol"))
    expected = protocol["smoke_test"]
    if smoke_result.get("protocol_version") != protocol.get("version", _EXTENSION_PROTOCOL_VERSION):
        return False
    if list(smoke_result.get("python_modules") or []) != list(expected.get("python_modules") or []):
        return False
    expected_paths = list(expected.get("required_paths") or [])
    if list(smoke_result.get("required_paths") or []) != expected_paths:
        return False
    install_path = Path(str((record.get("source") or {}).get("install_path") or "")).expanduser()
    if not install_path.is_dir():
        return False
    try:
        root = install_path.resolve()
    except OSError:
        return False
    for rel_path in expected_paths:
        try:
            path = (root / rel_path).resolve()
        except OSError:
            return False
        if not path.is_relative_to(root) or not path.exists():
            return False
    return True


def list_extensions(*, include_hidden: bool = False) -> list[dict[str, Any]]:
    fingerprint = store_fingerprint()
    key = (fingerprint, include_hidden)
    cached = _projection_cache_get("list_extensions", key)
    if cached is not None:
        return cached
    data = _load()
    return _projection_cache_put(
        "list_extensions",
        key,
        _list_extensions_from_data(data, include_hidden=include_hidden),
    )


def list_extensions_with_reconciliation(*, include_hidden: bool = False) -> tuple[list[dict[str, Any]], bool]:
    global _RECONCILED_STORE_FINGERPRINT
    path_key = str(_store_path())
    fingerprint = store_fingerprint()
    with _RECONCILED_STORE_LOCK:
        reconciled = _RECONCILED_STORE_FINGERPRINT == (path_key, fingerprint)
    if reconciled:
        return list_extensions(include_hidden=include_hidden), False

    data, _changed, public_changed = _load_with_changes()
    with _RECONCILED_STORE_LOCK:
        _RECONCILED_STORE_FINGERPRINT = (path_key, store_fingerprint())
    fingerprint = store_fingerprint()
    key = (fingerprint, include_hidden)
    return _projection_cache_put(
        "list_extensions",
        key,
        _list_extensions_from_data(data, include_hidden=include_hidden),
    ), public_changed


def _list_extensions_from_data(data: dict[str, Any], *, include_hidden: bool = False) -> list[dict[str, Any]]:
    return sorted(
        (
            record
            for extension_id, record in data["extensions"].items()
            if include_hidden or extension_id not in PUBLIC_EXTENSION_LIST_HIDDEN_IDS
        ),
        key=lambda item: item["manifest"]["id"],
    )


def _active_records() -> list[dict[str, Any]]:
    return _active_records_from_data(_load())


def get_extension(extension_id: str) -> dict[str, Any] | None:
    """Fingerprint-cached single-record read.

    HOT PATH: called on the per-request internal-extension auth chain
    (`internal_extension_settings`, `_require_extension_permission`) AND
    indirectly via `is_extension_active`, so each guarded request used to
    take the cross-process `fcntl.flock(LOCK_EX)` + disk read in `_load()`
    twice on the event loop. The faulthandler watchdog ranked
    `extension_store._store_lock` the #3 event-loop blocker (acquire-wait
    via `contextlib.__enter__`). Cache by `store_fingerprint()`
    (mtime_ns, size) exactly like `is_extension_enabled_cached`: any
    `_write_store_unlocked` bumps the file fingerprint and auto-
    invalidates, and `_clear_projection_cache()` drops it explicitly for
    same-fingerprint refreshes. Returns a deepcopy so callers can't mutate
    the shared snapshot (parity with the projection cache)."""
    fingerprint = store_fingerprint()
    with _GET_EXTENSION_CACHE_LOCK:
        cached = _GET_EXTENSION_CACHE.get(extension_id)
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])
    data = _load()
    record = data["extensions"].get(extension_id)
    with _GET_EXTENSION_CACHE_LOCK:
        _GET_EXTENSION_CACHE[extension_id] = (fingerprint, record)
    return copy.deepcopy(record)


def is_extension_enabled_cached(extension_id: str | None) -> bool:
    if not extension_id:
        return False
    fingerprint = store_fingerprint()
    with _ENABLED_CACHE_LOCK:
        cached = _ENABLED_CACHE.get(extension_id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    record = get_extension(extension_id)
    enabled = bool(record and record.get("enabled") is True)
    with _ENABLED_CACHE_LOCK:
        _ENABLED_CACHE[extension_id] = (fingerprint, enabled)
    return enabled


def _stored_capability_entrypoints(record: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    raw = entrypoints.get("capabilities") or []
    if not isinstance(raw, list):
        raise ExtensionError("stored extension entrypoints.capabilities must be a list")
    return [item for item in raw if isinstance(item, dict)]


def capability_catalog() -> dict[str, dict[str, Any]]:
    """Full-id (``<extension_id>:<cap_id>``) -> descriptor for every active
    extension. Single source for load/release validation, scope gating, and the
    post-turn release sweep."""
    catalog: dict[str, dict[str, Any]] = {}
    for record in _active_records():
        extension_id = record["manifest"]["id"]
        for item in _stored_capability_entrypoints(record):
            cid = str(item.get("id") or "").strip()
            if not cid:
                continue
            catalog[f"{extension_id}:{cid}"] = {
                **item,
                "id": f"{extension_id}:{cid}",
                "extension_id": extension_id,
            }
    return catalog


def get_capability(full_id: str) -> dict[str, Any] | None:
    return capability_catalog().get(str(full_id or "").strip())


def is_extension_active(extension_id: str) -> bool:
    record = get_extension(extension_id)
    if not record or record.get("enabled") is not True:
        return False
    return _entitlement_active(record.get("entitlement") or {})


def declared_permissions(record: dict[str, Any]) -> dict[str, Any]:
    """Manifest permission declarations: value is True (required), "optional", or a scope list."""
    return dict(((record.get("manifest") or {}).get("permissions") or {}))


def permission_grants(record: dict[str, Any]) -> dict[str, bool]:
    """User allow/forbid choices for optional permissions (fail-closed: absent = forbidden)."""
    raw = record.get("permission_grants") or {}
    return {str(k): bool(v) for k, v in raw.items() if v}


def has_permission(record: dict[str, Any], permission: str) -> bool:
    """Whether a permission is currently active for the extension.

    Required (declared True) -> always active. Optional (declared "optional")
    -> active only if the user granted it. Scope-list permissions
    (mutates_session_fields) are required-by-declaration and handled at their
    own sites, not here.
    """
    declared = declared_permissions(record).get(permission)
    if declared is True:
        return True
    if declared == "optional":
        return permission_grants(record).get(permission) is True
    return False


def effective_permissions(record: dict[str, Any]) -> dict[str, bool]:
    """All currently-active boolean permissions (required + granted optional) as {perm: True}."""
    active: dict[str, bool] = {}
    for perm, declared in declared_permissions(record).items():
        if declared is True:
            active[perm] = True
        elif declared == "optional" and permission_grants(record).get(perm) is True:
            active[perm] = True
    return active


def optional_permissions(record: dict[str, Any]) -> list[str]:
    """Boolean permissions declared optional (user-controllable allow/forbid)."""
    return sorted(p for p, v in declared_permissions(record).items() if v == "optional")


def set_permission_grant(extension_id: str, permission: str, granted: bool) -> dict[str, Any]:
    """Allow/forbid an optional permission for an extension. Required perms can't be toggled."""
    data = _load()
    record = data["extensions"].get(extension_id)
    if not record:
        raise ExtensionError("Extension not installed")
    declared = declared_permissions(record).get(permission)
    if declared != "optional":
        raise ExtensionError(f"Permission {permission!r} is not optional for this extension")
    grants = permission_grants(record)
    if granted:
        grants[permission] = True
    else:
        grants.pop(permission, None)
    record["permission_grants"] = grants
    record["updated_at"] = _now()
    _save(data)
    return record


_FIRST_PARTY_SOURCE_TYPES = frozenset({
    "better_agent_bundled",
    "better_agent_local",
    "better_agent_signed",
})


def is_first_party(record: dict[str, Any]) -> bool:
    """True when Better Agent itself ships/vouches for this extension: bundled in
    the release (``better_agent_bundled``), sourced from the local private repo on
    a dev machine (``better_agent_local``), or signed-delivered from the
    marketplace (``better_agent_signed``). First-party extensions are
    consent-exempt and are the ONLY extensions allowed to run in-process.
    Third-party sources (marketplace/git/artifact) are never first-party. The
    source type is bound to the installer that ran and is never read from the
    package, so a shipped extension cannot forge it."""
    return (record.get("source") or {}).get("type") in _FIRST_PARTY_SOURCE_TYPES


def permission_consent_fingerprint(record: dict[str, Any]) -> str:
    """Stable hash of the declared permission set. Re-consent is required when
    this changes (an update that asks for new permissions)."""
    declared = declared_permissions(record)
    payload = json.dumps({k: declared[k] for k in sorted(declared)}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def consent_required(record: dict[str, Any]) -> bool:
    """Whether the user must explicitly consent to this extension's declared
    permissions before it can be enabled. Builtins are first-party and never
    prompt; everything else must consent, and re-consent when permissions
    change (fingerprint mismatch). Fail-closed: unknown/empty consent → required."""
    if is_first_party(record):
        return False
    consent = record.get("consent") or {}
    return consent.get("fingerprint") != permission_consent_fingerprint(record)


def grant_consent(extension_id: str) -> dict[str, Any]:
    """Record the user's consent to the extension's current declared permission
    set. Must be called before set_enabled(True) for non-builtin extensions."""
    data = _load()
    record = data["extensions"].get(extension_id)
    if not record:
        raise ExtensionError("Extension not installed")
    record["consent"] = {
        "fingerprint": permission_consent_fingerprint(record),
        "at": _now(),
    }
    record["updated_at"] = _now()
    _save(data)
    return record


def install_from_repo(
    *,
    repo_url: str,
    extension_path: str,
    ref: str = "",
    entitlement_token: str = "",
) -> dict[str, Any]:
    repo_url = _validate_repo_url(repo_url)
    extension_path = _clean_rel_path(extension_path, field="extension_path")
    ref = str(ref or "").strip()
    if ref and not _VERSION_RE.fullmatch(ref):
        raise ExtensionError("ref contains invalid characters")

    tmp_parent = ba_home() / "extensions" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=tmp_parent) as tmp:
        clone_dir = Path(tmp) / "repo"
        clone_args = ["clone", "--depth", "1"]
        if ref:
            clone_args.extend(["--branch", ref])
        clone_args.extend([repo_url, str(clone_dir)])
        _git(clone_args)
        commit_sha = _git(["rev-parse", "HEAD"], cwd=clone_dir)
        package_dir = (clone_dir / extension_path).resolve()
        clone_root = clone_dir.resolve()
        if not package_dir.is_relative_to(clone_root) or not package_dir.is_dir():
            raise ExtensionError("extension_path not found in cloned repository")
        manifest_path = package_dir / "better-agent-extension.json"
        if not manifest_path.exists():
            raise ExtensionError("better-agent-extension.json not found at extension_path")
        manifest_id = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("id") or "")
        existing = _load()["extensions"].get(manifest_id)
        return _install_from_package_dir(
            package_dir=package_dir,
            source={
                "type": "git",
                "repo_url": repo_url,
                "extension_path": extension_path,
                "ref": ref,
                "commit_sha": commit_sha,
            },
            entitlement_token=entitlement_token,
            force_enabled=manifest_id in REQUIRED_EXTENSION_IDS,
            persist=True,
            existing_record=existing,
        )


def install_from_artifact(
    *,
    artifact_url: str,
    artifact_sha256: str,
    artifact_signature: str,
    entitlement_token: str = "",
    expected_extension_id: str = "",
    expected_version: str = "",
    source_type: str = "artifact",
    metadata_url: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    artifact_url = _validate_artifact_url(artifact_url)
    artifact_sha256 = str(artifact_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise ExtensionError("artifact_sha256 must be a sha256 hex digest")
    archive_bytes = _download_artifact(artifact_url)
    actual_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if actual_sha256 != artifact_sha256:
        raise ExtensionError("marketplace artifact digest mismatch")

    tmp_parent = ba_home() / "extensions" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="artifact-", dir=tmp_parent) as tmp:
        package_dir = Path(tmp) / "package"
        _safe_extract_tar_gz(archive_bytes, package_dir)
        manifest_path = package_dir / "better-agent-extension.json"
        if not manifest_path.exists():
            raise ExtensionError("better-agent-extension.json not found in marketplace artifact")
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        if expected_extension_id and manifest["id"] != expected_extension_id:
            raise ExtensionError("marketplace artifact extension id does not match metadata")
        if expected_version and manifest["version"] != expected_version:
            raise ExtensionError("marketplace artifact version does not match metadata")
        existing = _load()["extensions"].get(manifest["id"]) if persist else None
        _verify_artifact_signature(
            extension_id=manifest["id"],
            version=manifest["version"],
            artifact_sha256=artifact_sha256,
            signature=artifact_signature,
        )
        return _install_from_package_dir(
            package_dir=package_dir,
            source={
                "type": source_type,
                "repo_url": artifact_url,
                "extension_path": "",
                "ref": "",
                "commit_sha": artifact_sha256,
                "artifact_sha256": artifact_sha256,
                "artifact_url": artifact_url,
                "metadata_url": metadata_url,
            },
            entitlement_token=entitlement_token,
            force_enabled=manifest["id"] in REQUIRED_EXTENSION_IDS,
            persist=persist,
            existing_record=existing,
        )


def install_from_marketplace_metadata(
    *,
    metadata: dict[str, Any] | None = None,
    metadata_url: str = "",
    entitlement_token: str = "",
    source_type: str = "marketplace",
) -> dict[str, Any]:
    metadata_url = str(metadata_url or "").strip()
    if metadata_url:
        if metadata is not None:
            raise ExtensionError("Provide either marketplace metadata or metadata_url, not both")
        metadata = _fetch_json(metadata_url)
    if not isinstance(metadata, dict):
        raise ExtensionError("marketplace metadata is required")
    return install_from_artifact(
        artifact_url=str(metadata.get("artifact_url") or ""),
        artifact_sha256=str(metadata.get("artifact_sha256") or ""),
        artifact_signature=str(metadata.get("signature") or metadata.get("artifact_signature") or ""),
        entitlement_token=entitlement_token,
        expected_extension_id=str(metadata.get("extension_id") or metadata.get("id") or ""),
        expected_version=str(metadata.get("version") or ""),
        source_type=source_type,
        metadata_url=metadata_url,
    )


def _git_remote_commit(repo_url: str, ref: str) -> str:
    target = str(ref or "").strip() or "HEAD"
    output = _git(["ls-remote", repo_url, target])
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{40}", parts[0]):
            if parts[1].endswith("^{}"):
                continue
            return parts[0]
    raise ExtensionError("git remote ref not found")


def _marketplace_metadata_url_for_record(extension_id: str, record: dict[str, Any]) -> str:
    source = record.get("source") or {}
    return str(source.get("metadata_url") or _required_marketplace_metadata_url(extension_id)).strip()


def _update_git_extension(extension_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    source = record.get("source") or {}
    repo_url = _validate_repo_url(str(source.get("repo_url") or ""))
    extension_path = _clean_rel_path(str(source.get("extension_path") or ""), field="extension_path")
    ref = str(source.get("ref") or "").strip()
    remote_commit = _git_remote_commit(repo_url, ref)
    installed_commit = str(source.get("commit_sha") or "").strip()
    if remote_commit and installed_commit == remote_commit:
        return None
    return install_from_repo(repo_url=repo_url, extension_path=extension_path, ref=ref)


def _update_marketplace_extension(extension_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    source = record.get("source") or {}
    source_type = str(source.get("type") or "marketplace")
    metadata_url = _marketplace_metadata_url_for_record(extension_id, record)
    metadata = _fetch_json(metadata_url)
    published_sha = str(metadata.get("artifact_sha256") or "").strip().lower()
    installed_sha = str(source.get("artifact_sha256") or source.get("commit_sha") or "").strip().lower()
    if published_sha and installed_sha == published_sha:
        return None
    return install_from_marketplace_metadata(metadata_url=metadata_url, source_type=source_type)


def update_installed_extensions() -> dict[str, Any]:
    data = _load()
    results: list[dict[str, Any]] = []
    updated_count = 0
    considered = 0
    for extension_id, record in sorted(data["extensions"].items()):
        source = record.get("source") or {}
        source_type = str(source.get("type") or "")
        if source_type not in {"git", "marketplace", "better_agent_signed"}:
            continue
        considered += 1
        try:
            if source_type == "git":
                updated = _update_git_extension(extension_id, record)
            else:
                updated = _update_marketplace_extension(extension_id, record)
        except ExtensionError as exc:
            results.append({
                "extension_id": extension_id,
                "source_type": source_type,
                "updated": False,
                "error": str(exc),
            })
            continue
        if updated is None:
            results.append({
                "extension_id": extension_id,
                "source_type": source_type,
                "updated": False,
                "skipped": "up_to_date",
            })
            continue
        updated_count += 1
        results.append({
            "extension_id": extension_id,
            "source_type": source_type,
            "updated": True,
            "version": updated["manifest"].get("version", ""),
        })
    return {
        "considered": considered,
        "updated": updated_count,
        "results": results,
    }


def set_enabled(extension_id: str, enabled: bool) -> dict[str, Any]:
    data = _load()
    record = data["extensions"].get(extension_id)
    if not record:
        raise ExtensionError("Extension not installed")
    if extension_id in REQUIRED_EXTENSION_IDS and not enabled:
        raise ExtensionError("Required extension cannot be disabled")
    manifest = record.get("manifest") or {}
    if enabled:
        entitlement = record.get("entitlement") or {}
        if not _entitlement_active(entitlement):
            raise ExtensionError("Extension entitlement is not active")
        # Trusted-by-install: a non-builtin extension cannot be enabled until the
        # user has consented to its declared permission set. Fail closed.
        if consent_required(record):
            raise ExtensionConsentRequired(
                "Extension requires permission consent before it can be enabled"
            )
        # Fail closed: every declared dependency must be installed + active.
        missing = []
        for dep in manifest.get("dependencies", []):
            dep_rec = data["extensions"].get(dep)
            if not dep_rec or dep_rec.get("enabled") is not True or not _entitlement_active(dep_rec.get("entitlement") or {}):
                missing.append(dep)
        if missing:
            raise ExtensionError(
                f"Extension depends on extensions that are not active: {', '.join(missing)}"
            )
    else:
        # Fail closed: refuse to disable while another active extension depends on it.
        dependents = []
        for rec in data["extensions"].values():
            other_manifest = rec.get("manifest") or {}
            if other_manifest.get("id") == extension_id:
                continue
            if rec.get("enabled") is True and extension_id in other_manifest.get("dependencies", []):
                dependents.append(other_manifest.get("id", ""))
        if dependents:
            raise ExtensionError(
                f"Cannot disable: active extensions depend on it: {', '.join(dependents)}"
            )
    record["enabled"] = bool(enabled)
    record["updated_at"] = _now()
    _save(data)
    _evict_extension_backend(extension_id)
    extension_instructions.reconcile_blocks(record)
    extension_applied_config.reconcile(record)
    reconcile_runtime_skills()
    reconcile_native_mcp_servers()
    import extension_token_registry
    if bool(enabled):
        if has_permission(record, "internal_loopback"):
            extension_token_registry.mint(extension_id)
    else:
        # Revoke so a disabled extension's token stops authenticating immediately.
        extension_token_registry.revoke(extension_id)
    return record


def set_instruction_enabled(
    extension_id: str, *, level: str, enabled: bool, project_path: str = ""
) -> dict[str, Any]:
    """Toggle an extension's instruction injection at a level (global or a project)."""
    if level not in _INSTRUCTION_LEVELS:
        raise ExtensionError(f"level must be one of {sorted(_INSTRUCTION_LEVELS)}")
    data = _load()
    record = data["extensions"].get(extension_id)
    if not record:
        raise ExtensionError("Extension not installed")
    state = extension_instructions.normalize_state(record)
    if level == "global":
        state["global"] = bool(enabled)
    else:
        if not project_path:
            raise ExtensionError("project level requires project_path")
        resolved = Path(project_path).expanduser().resolve()
        known = {str(p) for p in extension_instructions._local_project_paths()}
        if str(resolved) not in known:
            raise ExtensionError("project_path is not a known local project")
        if enabled:
            state["projects"][str(resolved)] = True
        else:
            state["projects"].pop(str(resolved), None)
    record["instructions_enabled"] = state
    record["updated_at"] = _now()
    _save(data)
    extension_instructions.reconcile_blocks(record)
    return record


def reconcile_all_instructions() -> None:
    """Reconcile every installed extension's instruction blocks and sweep orphans.

    Self-heals the provider instruction files: re-applies enabled extensions,
    purges disabled ones from every file, and removes blocks owned by extensions
    no longer installed. Run on backend startup so on-disk blocks can't drift
    from the store.
    """
    data = _load()
    installed_ids: set[str] = set()
    for record in data["extensions"].values():
        manifest = record.get("manifest") or {}
        if manifest.get("id"):
            installed_ids.add(manifest["id"])
        extension_instructions.reconcile_blocks(record)
    extension_applied_config.reconcile_all()
    return extension_instructions.sweep_orphan_blocks(installed_ids)


def reconcile_runtime_skills() -> int:
    data = _load()
    settings = _load_ext_settings()
    root = Path.home() / ".agents" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    active_native_skill_names: dict[str, str] = {}
    for record in _active_records_from_data(data):
        if harness_delivery_mode(record["manifest"]["id"], settings=settings) == _HARNESS_DELIVERY_RUNTIME:
            continue
        manifest = record["manifest"]
        for item in manifest.get("entrypoints", {}).get("skills") or []:
            active_native_skill_names[item["name"]] = manifest["id"]
    removed = _purge_extension_runtime_skills(root, active_native_skill_names)
    installed = 0
    for record in _active_records_from_data(data):
        if harness_delivery_mode(record["manifest"]["id"], settings=settings) == _HARNESS_DELIVERY_RUNTIME:
            continue
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        manifest = record["manifest"]
        extension_id = manifest["id"]
        for item in manifest.get("entrypoints", {}).get("skills") or []:
            source = (install_root / item["path"]).resolve()
            if not source.is_relative_to(install_root):
                continue
            if not source.is_dir() or not (source / "SKILL.md").is_file():
                continue
            target = root / item["name"]
            if _runtime_skill_owner(target) == extension_id:
                continue
            _replace_runtime_skill_dir(source, target, extension_id)
            installed += 1
    return removed + installed


def runtime_skill_entries() -> list[dict[str, str]]:
    skills: list[dict[str, str]] = []
    data = _load()
    settings = _load_ext_settings()
    for record in _active_records_from_data(data):
        manifest = record["manifest"]
        delivery = harness_delivery_mode(manifest["id"], settings=settings)
        source = record.get("source") or {}
        include_native_direct = (
            delivery == _HARNESS_DELIVERY_NATIVE
            and source.get("type") == "better_agent_local"
            and private_local_runtime_mode() == _PRIVATE_LOCAL_RUNTIME_SOURCE
            and _private_local_source_root(source) is not None
        )
        if delivery != _HARNESS_DELIVERY_RUNTIME and not include_native_direct:
            continue
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        for item in manifest.get("entrypoints", {}).get("skills") or []:
            source = (install_root / item["path"]).resolve()
            if not source.is_relative_to(install_root):
                continue
            skill_md = source / "SKILL.md"
            if not source.is_dir() or not skill_md.is_file():
                continue
            skills.append({
                "name": item["name"],
                "dir": str(source),
                "path": str(skill_md),
            })
    return skills


def _active_records_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (
            record
            for record in data["extensions"].values()
            if _record_active(record)
        ),
        key=lambda item: item["manifest"]["id"],
    )


def _purge_extension_runtime_skills(root: Path, active_native_skill_names: dict[str, str]) -> int:
    count = 0
    if not root.is_dir():
        return count
    for child in root.iterdir():
        owner = _runtime_skill_owner(child)
        if not owner:
            continue
        if active_native_skill_names.get(child.name) == owner:
            continue
        _remove_runtime_skill_path(child)
        count += 1
    return count


def _runtime_skill_owner(path: Path) -> str:
    marker = path / _RUNTIME_SKILL_OWNER_FILE
    if not marker.is_file():
        return ""
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _replace_runtime_skill_dir(source: Path, target: Path, extension_id: str) -> None:
    if target.exists() or target.is_symlink():
        _remove_runtime_skill_path(target)
    shutil.copytree(source, target, symlinks=True)
    (target / _RUNTIME_SKILL_OWNER_FILE).write_text(extension_id + "\n", encoding="utf-8")


def _remove_runtime_skill_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)


def uninstall(extension_id: str) -> None:
    if extension_id in REQUIRED_EXTENSION_IDS:
        raise ExtensionError("Required extension cannot be uninstalled")
    data = _load()
    record = data["extensions"].pop(extension_id, None)
    if not record:
        raise ExtensionError("Extension not installed")
    if extension_id == BUILTIN_ASSISTANT_EXTENSION_ID:
        import assistant_ui
        assistant_ui.cleanup_singleton()
    _evict_extension_backend(extension_id)
    extension_instructions.clear_all_blocks(record)
    extension_applied_config.clear_for_uninstall(record)
    source = record.get("source") or {}
    install_path = Path(str(source.get("install_path") or ""))
    root = _install_root().resolve()
    if install_path and install_path.exists():
        resolved = install_path.resolve()
        if resolved.is_relative_to(root):
            extension_root = resolved.parent.parent if resolved.parent.name == "versions" else resolved
            if not extension_root.is_relative_to(root):
                raise ExtensionError("Extension install path escapes install root")
            shutil.rmtree(extension_root)
    _save(data, deleted_extension_ids={extension_id})
    import extension_token_registry
    extension_token_registry.revoke(extension_id)
    reconcile_runtime_skills()
    reconcile_native_mcp_servers()


def team_definition_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for record in list_extensions():
        if not record.get("enabled"):
            continue
        entitlement = record.get("entitlement") or {}
        if not _entitlement_active(entitlement):
            continue
        manifest = record["manifest"]
        definitions = manifest.get("entrypoints", {}).get("team_definitions") or []
        install_path = str(record.get("source", {}).get("install_path") or "")
        if not install_path:
            continue
        install_root = Path(install_path).resolve()
        for item in definitions:
            name = item["name"]
            path = (install_root / item["path"]).resolve()
            if not path.is_relative_to(install_root) or not path.is_file():
                continue
            sources.append(
                {
                    "source_id": f"extension:{manifest['id']}:{name}",
                    "extension_id": manifest["id"],
                    "extension_name": manifest["name"],
                    "name": name,
                    "path": str(path),
                    "definition": json.loads(path.read_text(encoding="utf-8")),
                }
            )
    return sources


def _sdk_pythonpath() -> str:
    """Absolute path to the shared ``sdk/`` dir, or "" if absent.

    Put on extension subprocess PYTHONPATH so they can ``import
    better_agent_sdk``. Only ``sdk/`` is exposed — never ``backend/`` — so
    extensions still cannot import core modules directly (sandbox preserved)."""
    sdk_root = Path(__file__).resolve().parent.parent / "sdk"
    return str(sdk_root) if sdk_root.is_dir() else ""


def is_reserved_mcp_server_name(name: str) -> bool:
    return name in _RESERVED_MCP_SERVER_NAMES


def _disabled_runtime_extension_ids(inputs: dict[str, Any]) -> set[str]:
    raw = inputs.get("disabled_builtin_extensions")
    if not isinstance(raw, list):
        return set()
    return {
        extension_id
        for extension_id in (str(item or "").strip() for item in raw)
        if extension_id
    }


def runtime_mcp_server_configs(
    inputs: dict[str, Any],
    *,
    user_facing: bool,
    bare: bool,
) -> dict[str, dict[str, Any]]:
    return _mcp_server_configs_for_delivery(
        _HARNESS_DELIVERY_RUNTIME,
        inputs,
        user_facing=user_facing,
        bare=bare,
    )


def native_mcp_server_configs(
    inputs: dict[str, Any],
    *,
    user_facing: bool,
    bare: bool,
) -> dict[str, dict[str, Any]]:
    return _mcp_server_configs_for_delivery(
        _HARNESS_DELIVERY_NATIVE,
        inputs,
        user_facing=user_facing,
        bare=bare,
    )


def native_mcp_launcher_server_configs(
    inputs: dict[str, Any],
    *,
    user_facing: bool,
    bare: bool,
) -> dict[str, dict[str, Any]]:
    return _mcp_server_configs_for_delivery(
        _HARNESS_DELIVERY_NATIVE,
        inputs,
        user_facing=user_facing,
        bare=bare,
        launcher=True,
    )


def _mcp_server_configs_for_delivery(
    delivery: str,
    inputs: dict[str, Any],
    *,
    user_facing: bool,
    bare: bool,
    launcher: bool = False,
) -> dict[str, dict[str, Any]]:
    resolved_inputs = {
        **inputs,
        "open_file_panel_enabled": bool(user_facing),
        "bare_config": bool(bare),
    }
    disabled_extension_ids = _disabled_runtime_extension_ids(inputs)
    configs: dict[str, dict[str, Any]] = {}
    for record in _active_records():
        if harness_delivery_mode(record["manifest"]["id"]) != delivery:
            continue
        if not _record_runtime_ready(record):
            continue
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        manifest = record["manifest"]
        if manifest["id"] in disabled_extension_ids:
            continue
        for item in _stored_mcp_entrypoints(record):
            server_name = item.get("replaces_builtin") or item["name"]
            if launcher:
                if not _mcp_item_available_for_inputs(record, item, resolved_inputs):
                    continue
                config = extension_mcp.launcher_server_item(manifest["id"], item["name"])
                config.update(_mcp_tool_timeout_config(manifest, item))
                config["env"] = {
                    **dict(config.get("env") or {}),
                    **_native_mcp_launcher_env(resolved_inputs),
                }
            else:
                config = _runtime_mcp_server_config_for_item(record, item, resolved_inputs)
            if config:
                configs[server_name] = config
    return configs


def _native_mcp_launcher_env(inputs: dict[str, Any]) -> dict[str, str]:
    backend_url = str(
        inputs.get("backend_url")
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    disabled_extensions = [
        str(item).strip()
        for item in inputs.get("disabled_builtin_extensions") or []
        if str(item or "").strip()
    ]
    # The launcher subprocess re-resolves the MCP config from env via
    # `_runtime_inputs()`, then re-evaluates the manifest predicate. A predicate
    # that gates on the per-session active-capability set (e.g. testape's
    # `contains: {active_capability_ids: ...}`) would otherwise fail closed in
    # the launcher path — the entry is built (cap active at build time) but the
    # subprocess has no active set to match — so a loaded capability's MCP would
    # advertise tools whose server then refuses to start. Thread the active set
    # through so the launcher predicate evaluates identically to the in-process
    # path. Comma-joined; ids never contain commas (validated `<ext_id>:<cap_id>`).
    active_capability_ids = [
        str(item).strip()
        for item in inputs.get("active_capability_ids") or []
        if str(item or "").strip()
    ]
    provisioned_tool_profile = str(inputs.get("provisioned_tool_profile") or "").strip()
    return dual_env_many({
        "BETTER_CLAUDE_BACKEND_URL": backend_url,
        "BETTER_CLAUDE_APP_SESSION_ID": str(inputs.get("app_session_id") or ""),
        "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
        "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
        "BETTER_CLAUDE_PROVIDER_ID": str(inputs.get("provider_id") or ""),
        "BETTER_CLAUDE_MODE": str(inputs.get("mode") or ""),
        "BETTER_CLAUDE_WORKING_MODE": str(inputs.get("working_mode") or ""),
        "BETTER_CLAUDE_BARE_CONFIG": "1" if inputs.get("bare_config") else "0",
        "BETTER_CLAUDE_USER_FACING": "1"
        if bool(inputs.get("open_file_panel_enabled")) and not bool(inputs.get("bare_config"))
        else "0",
        "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS": ",".join(disabled_extensions),
        "BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS": ",".join(active_capability_ids),
        "BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE": provisioned_tool_profile,
    })


def resolve_native_mcp_server_config(
    *,
    extension_id: str,
    server_name: str,
    inputs: dict[str, Any],
) -> dict[str, Any] | None:
    record = get_extension(extension_id)
    if not record or not _record_active(record) or not _record_runtime_ready(record):
        return None
    if harness_delivery_mode(extension_id) != _HARNESS_DELIVERY_NATIVE:
        return None
    manifest = record.get("manifest") or {}
    if manifest["id"] in _disabled_runtime_extension_ids(inputs):
        return None
    item = None
    for candidate in _stored_mcp_entrypoints(record):
        if str(candidate.get("name") or "") == server_name:
            item = candidate
            break
    if item is None:
        return None
    return _runtime_mcp_server_config_for_item(record, item, inputs)


def _runtime_mcp_server_config_for_item(
    record: dict[str, Any],
    item: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any] | None:
    install_root = runtime_package_root_for_record(record)
    if install_root is None or not install_root.exists():
        return None
    manifest = record["manifest"]
    backend_url = str(
        inputs.get("backend_url")
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    internal_token = ""
    base_env = dual_env_many({
        "BETTER_CLAUDE_BACKEND_URL": backend_url,
        "BETTER_CLAUDE_APP_SESSION_ID": str(inputs.get("app_session_id") or ""),
        "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
        "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
        "BETTER_CLAUDE_PROVIDER_ID": str(inputs.get("provider_id") or ""),
    })
    if has_permission(record, "internal_loopback"):
        # Per-extension token: identity is derived from this secret, never
        # from a self-asserted X-Extension-Id header. The global token from
        # `inputs` is intentionally ignored here.
        try:
            from orchestrator import get_active_coordinator
            coordinator = get_active_coordinator()
        except Exception:
            coordinator = None
        if coordinator is not None:
            internal_token = coordinator.mint_extension_token(str(manifest["id"]))
        else:
            import extension_token_registry
            internal_token = extension_token_registry.mint(str(manifest["id"]))
        base_env.update(dual_env_many({"BETTER_CLAUDE_INTERNAL_TOKEN": internal_token}))
    if not _mcp_item_available_for_inputs(record, item, inputs):
        return None
    if item.get("name") in _RESERVED_MCP_SERVER_NAMES:
        return None
    if not item.get("python") and not item.get("module") and not item.get("command"):
        return None
    env = {
        **base_env,
        **dict(item.get("env") or {}),
        **dual_env_many({"BETTER_CLAUDE_EXTENSION_ID": manifest["id"]}),
    }
    if manifest["id"] == BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID:
        from provider_config_sync_api import provider_config_sync_mcp_env
        env.update(provider_config_sync_mcp_env(
            backend_url=backend_url,
            internal_token=internal_token,
        ))
    timeout_config = _mcp_tool_timeout_config(manifest, item)
    command = str(item.get("command") or "").strip()
    if command:
        return {
            "command": command,
            "args": list(item.get("args") or []),
            "env": env,
            **timeout_config,
        }
    venv_bin = _venv_bin_dir(install_root / ".venv")
    if venv_bin.is_dir():
        existing_path = env.get("PATH") or os.environ.get("PATH") or ""
        env["PATH"] = str(venv_bin) + (os.pathsep + existing_path if existing_path else "")
    sdk_path = _sdk_pythonpath()
    pythonpath_parts = [str(install_root)]
    site_packages = _venv_site_packages_dir(install_root / ".venv")
    if site_packages is not None:
        pythonpath_parts.append(str(site_packages))
    if sdk_path:
        pythonpath_parts.append(sdk_path)
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    module = str(item.get("module") or "").strip()
    if module:
        return {
            "command": sys.executable,
            "args": ["-m", module, *list(item.get("args") or [])],
            "env": env,
            **timeout_config,
        }
    script = (install_root / item["python"]).resolve()
    if not script.is_relative_to(install_root) or not script.is_file():
        return None
    return {
        "command": sys.executable,
        "args": [str(script), *list(item.get("args") or [])],
        "env": env,
        **timeout_config,
    }


def _mcp_tool_timeout_config(manifest: dict[str, Any], item: dict[str, Any]) -> dict[str, float]:
    if (
        manifest["id"] == BUILTIN_REQUIREMENTS_EXTENSION_ID
        and (
            str(item.get("name") or "") == "better-agent-requirements"
            or str(item.get("replaces_builtin") or "") == "get-requirements"
        )
    ):
        return {"tool_timeout_sec": 760.0}
    return {}


def _mcp_item_available_for_inputs(
    record: dict[str, Any],
    item: dict[str, Any],
    inputs: dict[str, Any],
) -> bool:
    manifest = record["manifest"]
    if not item.get("python") and not item.get("module") and not item.get("command"):
        return False
    if not is_mcp_server_enabled(manifest["id"], item["name"]):
        return False
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("open_file_panel_enabled")) and not bare
    if item.get("user_facing") and not user_facing and not (bare and item.get("bare_allowed")):
        return False
    if bare and not item.get("bare_allowed"):
        return False
    backend_url = str(
        inputs.get("backend_url")
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    internal_token = str(inputs.get("internal_token") or "").strip()
    if item.get("requires_backend_auth") and not (backend_url and internal_token):
        return False
    predicate = item.get("predicate")
    if predicate and not _mcp_predicate_matches(predicate, inputs):
        return False
    return True


def reconcile_extension_tokens() -> int:
    """Pre-mint the per-extension internal-loopback token for every active
    extension that declares it. Done in-backend so an out-of-process native
    MCP launcher always reads an existing token from the registry instead of
    racing to create one. Idempotent — mint() is a no-op once the token exists."""
    import extension_token_registry
    minted = 0
    for record in _active_records():
        if has_permission(record, "internal_loopback"):
            extension_token_registry.mint(str(record["manifest"]["id"]))
            minted += 1
    return minted


def reconcile_extension_consent() -> int:
    """Grandfather migration: stamp consent on already-enabled non-builtin
    extensions that predate the consent feature, so they keep working without a
    re-prompt. Self-limiting and idempotent — install and set_enabled both
    enforce consent, so the only records this touches are legacy ones; once
    stamped, consent_required() is False and they're skipped."""
    data = _load()
    changed = 0
    for record in data["extensions"].values():
        if is_first_party(record) or record.get("enabled") is not True:
            continue
        if not consent_required(record):
            continue
        record["consent"] = {
            "fingerprint": permission_consent_fingerprint(record),
            "at": _now(),
        }
        changed += 1
    if changed:
        _save(data)
    return changed


def reconcile_native_mcp_servers() -> int:
    import config_store

    settings = _load_ext_settings()
    disabled_extension_ids = set(config_store.get_disabled_builtin_extensions())
    active_records = [
        record
        for record in _active_records()
        if record["manifest"]["id"] not in disabled_extension_ids
        and harness_delivery_mode(record["manifest"]["id"], settings=settings) == _HARNESS_DELIVERY_NATIVE
        and _record_runtime_ready(record)
    ]
    return extension_mcp.reconcile_native_mcp_servers(active_records)


def post_turn_hooks() -> list[tuple[str, str]]:
    """(extension_id, path) for active, runtime-ready INSTALLED extensions
    declaring a ``entrypoints.hooks.post_turn`` backend path."""
    out: list[tuple[str, str]] = []
    for record in list_extensions():
        if not _record_active(record):
            continue
        path = (record["manifest"].get("entrypoints") or {}).get("hooks", {}).get("post_turn")
        if not path or not _record_runtime_ready(record):
            continue
        out.append((record["manifest"]["id"], str(path)))
    return out


def pre_turn_hooks() -> list[tuple[str, str]]:
    """(extension_id, path) for active, runtime-ready INSTALLED extensions
    declaring a ``entrypoints.hooks.pre_turn`` backend path."""
    out: list[tuple[str, str]] = []
    for record in list_extensions():
        if not _record_active(record):
            continue
        path = (record["manifest"].get("entrypoints") or {}).get("hooks", {}).get("pre_turn")
        if not path or not _record_runtime_ready(record):
            continue
        out.append((record["manifest"]["id"], str(path)))
    return out


def session_event_hooks() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for record in list_extensions():
        if not _record_active(record):
            continue
        path = (record["manifest"].get("entrypoints") or {}).get("hooks", {}).get("session_event")
        if not path or not _record_runtime_ready(record):
            continue
        out.append((record["manifest"]["id"], str(path)))
    return out


def session_field_allowlist(extension_id: str) -> list[str]:
    """Session-record fields ``extension_id`` is permitted to mutate via the
    scoped /api/internal/session-field endpoint — the subset of
    ``_MUTABLE_SESSION_FIELDS`` it declared under permissions.mutates_session_fields."""
    record = get_extension(extension_id)
    if not record:
        return []
    declared = (record["manifest"].get("permissions") or {}).get("mutates_session_fields") or []
    return [f for f in declared if f in _MUTABLE_SESSION_FIELDS]


def session_field_read_allowlist(extension_id: str) -> list[str]:
    record = get_extension(extension_id)
    if not record:
        return []
    declared = (record["manifest"].get("permissions") or {}).get("reads_session_fields") or []
    return [f for f in declared if f in _READABLE_SESSION_FIELDS]


def backend_entrypoint_spec(extension_id: str) -> dict[str, Any] | None:
    record = get_extension(extension_id)
    if not record:
        return None
    if not _record_active(record) or not _record_backend_surface_ready(record):
        return None
    manifest = record["manifest"]
    entrypoints = manifest.get("entrypoints", {})
    backend_path = str(entrypoints.get("backend") or "")
    backend_module = str(entrypoints.get("backend_module") or "")
    if not backend_path and not backend_module:
        return None
    if not has_permission(record, "backend_routes"):
        return None
    install_root = runtime_package_root_for_record(record)
    if install_root is None:
        return None
    entrypoint = ""
    entrypoint_kind = "module" if backend_module else "file"
    if backend_path:
        backend_file = (install_root / backend_path).resolve()
        if not backend_file.is_relative_to(install_root) or not backend_file.is_file():
            return None
        entrypoint = str(backend_file)
    else:
        entrypoint = backend_module
    return {
        "extension_id": manifest["id"],
        "install_path": str(install_root),
        "entrypoint": entrypoint,
        "entrypoint_kind": entrypoint_kind,
        "backend_timeouts": dict(entrypoints.get("backend_timeouts") or {}),
        "backend_retry_on_exit": list(entrypoints.get("backend_retry_on_exit") or []),
        "prefix": f"/api/extensions/{manifest['id']}/backend",
        "permissions": dict(manifest.get("permissions") or {}),
        "effective_permissions": effective_permissions(record),
        "sdk_pythonpath": _sdk_pythonpath(),
        "source": {
            "type": str(record["source"].get("type") or ""),
            "repo_url": str(record["source"].get("repo_url") or ""),
            "extension_path": str(record["source"].get("extension_path") or ""),
            "ref": str(record["source"].get("ref") or ""),
            "commit_sha": str(record["source"].get("commit_sha") or ""),
        },
    }


def frontend_entrypoints() -> list[dict[str, Any]]:
    cached = _frontend_entrypoints_cached_for_current_files()
    if cached is not None:
        return cached
    key = frontend_entrypoints_cache_key()
    cached = _projection_cache_get("frontend_entrypoints", key)
    if cached is not None:
        return cached
    entries: list[dict[str, Any]] = []
    for record in _active_records():
        if not _record_active(record) or not _record_runtime_ready(record):
            continue
        manifest = record["manifest"]
        frontend_path = str(manifest.get("entrypoints", {}).get("frontend") or "")
        if not frontend_path:
            continue
        runtime_root = runtime_package_root_for_record(record)
        if runtime_root is None:
            continue
        entrypoint = (runtime_root / frontend_path).resolve()
        if not entrypoint.is_relative_to(runtime_root) or not entrypoint.is_file():
            continue
        # Bust browser/PWA caches when an extension changes. Packaged
        # extensions use their install commit. Local private source mode also
        # mixes in frontend asset fingerprints so live source edits get new
        # module URLs without reinstalling the extension.
        frontend_modules = [
            item
            for item in manifest.get("entrypoints", {}).get("frontend_modules") or []
            if is_frontend_module_enabled(manifest["id"], item["slot"], item["id"])
        ]
        frontend_assets = [frontend_path, *[str(item.get("module") or "") for item in frontend_modules]]
        v = _frontend_asset_version(record, runtime_root, frontend_assets)
        bust = f"?v={v}"
        entries.append(
            {
                "extension_id": manifest["id"],
                "name": manifest["name"],
                "entrypoint": frontend_path,
                "entrypoint_url": f"/api/extensions/{manifest['id']}/frontend/{frontend_path}{bust}",
                "payments": (manifest.get("permissions") or {}).get("payments") is True,
                "frontend_modules": [
                    {
                        "slot": item["slot"],
                        "id": item["id"],
                        "label": item["label"],
                        "kind": item["kind"],
                        "module": item["module"],
                        "module_url": f"/api/extensions/{manifest['id']}/frontend/{item['module']}{bust}",
                    }
                    for item in frontend_modules
                ],
            }
        )
    return _projection_cache_put("frontend_entrypoints", key, entries)


def _frontend_entrypoints_cached_for_current_files() -> list[dict[str, Any]] | None:
    fingerprint = store_fingerprint()
    settings_fp = extension_settings_fingerprint()
    mode = private_local_runtime_mode()
    for key, value in _projection_cache_items("frontend_entrypoints"):
        if len(key) != 4 or key[0] != fingerprint or key[1] != settings_fp or key[2] != mode:
            continue
        if mode != _PRIVATE_LOCAL_RUNTIME_SOURCE:
            return value
        source_fingerprints = key[3]
        if not isinstance(source_fingerprints, tuple):
            continue
        current: list[tuple[Any, ...]] = []
        valid = True
        for item in source_fingerprints:
            if not isinstance(item, tuple) or len(item) != 4:
                valid = False
                break
            extension_id, runtime_root, asset_paths, _old_fingerprint = item
            if not isinstance(asset_paths, tuple):
                valid = False
                break
            current.append(
                (
                    extension_id,
                    runtime_root,
                    asset_paths,
                    _frontend_assets_fingerprint(
                        Path(str(runtime_root)),
                        [str(path) for path in asset_paths],
                    ),
                )
            )
        if valid and tuple(current) == source_fingerprints:
            return value
    return None


def frontend_entrypoints_cache_key() -> tuple[Any, ...]:
    settings_fp = extension_settings_fingerprint()
    mode = private_local_runtime_mode()
    if mode != _PRIVATE_LOCAL_RUNTIME_SOURCE:
        return (store_fingerprint(), settings_fp, mode, ())
    source_fingerprints: list[tuple[Any, ...]] = []
    for record in _active_records():
        source = record.get("source") or {}
        if source.get("type") != "better_agent_local":
            continue
        manifest = record.get("manifest") or {}
        frontend_path = str(manifest.get("entrypoints", {}).get("frontend") or "")
        if not frontend_path:
            continue
        runtime_root = runtime_package_root_for_record(record)
        if runtime_root is None:
            continue
        frontend_modules = manifest.get("entrypoints", {}).get("frontend_modules") or []
        frontend_assets = [frontend_path, *[str(item.get("module") or "") for item in frontend_modules]]
        asset_paths = tuple(frontend_assets)
        source_fingerprints.append(
            (
                str(manifest.get("id") or ""),
                str(runtime_root),
                asset_paths,
                _frontend_assets_fingerprint(runtime_root, frontend_assets),
            )
        )
    return (store_fingerprint(), settings_fp, mode, tuple(source_fingerprints))


def _frontend_assets_fingerprint(runtime_root: Path, asset_paths: list[str]) -> tuple[tuple[str, int, int], ...]:
    fingerprints: list[tuple[str, int, int]] = []
    for raw in asset_paths:
        if not raw:
            continue
        try:
            rel_path = _clean_rel_path(raw, field="asset_path")
            path = (runtime_root / rel_path).resolve()
            if not path.is_relative_to(runtime_root) or not path.is_file():
                fingerprints.append((rel_path, -1, -1))
                continue
            stat = path.stat()
            fingerprints.append((rel_path, stat.st_mtime_ns, stat.st_size))
        except (ExtensionError, OSError):
            fingerprints.append((str(raw), -1, -1))
    return tuple(fingerprints)


def _frontend_asset_version(record: dict[str, Any], runtime_root: Path, asset_paths: list[str]) -> str:
    base = str((record.get("source") or {}).get("commit_sha") or "")[:12] or "unversioned"
    source = record.get("source") or {}
    if source.get("type") != "better_agent_local" or private_local_runtime_mode() != _PRIVATE_LOCAL_RUNTIME_SOURCE:
        return base
    digest = hashlib.sha256(repr(_frontend_assets_fingerprint(runtime_root, asset_paths)).encode("utf-8")).hexdigest()[:12]
    return f"{base}-{digest}"


def resolve_frontend_asset(extension_id: str, asset_path: str) -> Path:
    record = get_extension(extension_id)
    if not record:
        raise ExtensionError("Extension is not installed")
    if record.get("enabled") is not True or not _entitlement_active(record.get("entitlement") or {}):
        raise ExtensionError("Extension is not installed")
    frontend_path = str(record["manifest"].get("entrypoints", {}).get("frontend") or "")
    if not frontend_path:
        raise ExtensionError("Extension has no frontend entrypoint")
    requested = _clean_rel_path(asset_path or frontend_path, field="asset_path")
    runtime_root = runtime_package_root_for_record(record)
    if runtime_root is None:
        raise ExtensionError("Extension is not installed")
    frontend_entrypoint = (runtime_root / frontend_path).resolve()
    frontend_root = frontend_entrypoint.parent
    if frontend_root == runtime_root:
        raise ExtensionError("Extension frontend entrypoint must live under a dedicated asset directory")
    target = (runtime_root / requested).resolve()
    if not target.is_relative_to(frontend_root) or not target.is_file():
        raise ExtensionError("Extension frontend asset not found")
    return target


# ── extension UI hooks (quick_button / page) ─────────────────────────
#
# Manifest-declared UI surfaces (entrypoints.quick_button / entrypoints.page)
# the frontend renders data-driven. Each surface has a per-extension toggle
# (ui-settings.json) so a user can hide one without disabling the extension.

_UI_SETTINGS_SCHEMA_VERSION = 1


def _ui_settings_path() -> Path:
    return ba_home() / "extensions" / "ui-settings.json"


def _blank_ui_settings() -> dict[str, Any]:
    return {"schema_version": _UI_SETTINGS_SCHEMA_VERSION, "settings": {}}


def _load_ui_settings() -> dict[str, dict[str, Any]]:
    data = read_json(_ui_settings_path(), _blank_ui_settings())
    if data.get("schema_version") != _UI_SETTINGS_SCHEMA_VERSION:
        raise ExtensionError(
            "Unsupported extension ui-settings schema; wipe extensions/ui-settings.json to start fresh"
        )
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise ExtensionError("Malformed extension ui-settings: settings must be an object")
    return settings


def _save_ui_settings(settings: dict[str, dict[str, Any]]) -> None:
    write_json(_ui_settings_path(), {"schema_version": _UI_SETTINGS_SCHEMA_VERSION, "settings": settings})
    _clear_projection_cache()


def get_ui_settings(extension_id: str) -> dict[str, bool]:
    """Per-extension UI-surface toggles with enabled-by-default applied."""
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    raw = _load_ui_settings().get(extension_id, {})
    return {
        "quick_button_enabled": raw.get("quick_button_enabled", True),
        "page_enabled": raw.get("page_enabled", True),
    }


def set_ui_settings(
    extension_id: str,
    *,
    quick_button_enabled: bool | None = None,
    page_enabled: bool | None = None,
) -> dict[str, bool]:
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    settings = _load_ui_settings()
    current = dict(settings.get(extension_id, {}))
    if quick_button_enabled is not None:
        current["quick_button_enabled"] = bool(quick_button_enabled)
    if page_enabled is not None:
        current["page_enabled"] = bool(page_enabled)
    settings[extension_id] = current
    _save_ui_settings(settings)
    return {
        "quick_button_enabled": current.get("quick_button_enabled", True),
        "page_enabled": current.get("page_enabled", True),
    }


def _ui_hook_enabled(settings: dict[str, dict[str, Any]], extension_id: str, key: str) -> bool:
    return bool(settings.get(extension_id, {}).get(key, True))


# Quick-button supersession: while the superseding extension is active
# (installed + enabled + entitled), the superseded extension's quick button is
# hidden so the superseder's button takes its place. The button reappears the
# moment the superseder is uninstalled or disabled, provided the superseded
# extension is itself active and runtime-ready. A None superseder id (absent
# private registry in a pure-public checkout) never supersedes — fail open.
_QUICK_BUTTON_SUPERSEDED_BY: dict[str, str | None] = {
    BUILTIN_ASK_EXTENSION_ID: BUILTIN_ASSISTANT_EXTENSION_ID,
}


def _quick_button_superseded(extension_id: str) -> bool:
    superseder = _QUICK_BUTTON_SUPERSEDED_BY.get(extension_id)
    if not superseder:
        return False
    return is_extension_active(superseder)


def ui_hooks() -> dict[str, list[dict[str, Any]]]:
    """Quick buttons and pages for every active extension (built-ins
    included), filtered by per-extension UI-surface toggles."""
    key = ui_hooks_cache_key()
    cached = _projection_cache_get("ui_hooks", key)
    if cached is not None:
        return cached
    settings = _load_ui_settings()
    quick_buttons: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for record in list_extensions():
        manifest = record["manifest"]
        extension_id = manifest["id"]
        if not _record_active(record) or not _record_runtime_ready(record):
            continue
        extension_name = manifest.get("name") or extension_id
        entrypoints = manifest.get("entrypoints") or {}

        quick_button = entrypoints.get("quick_button") or {}
        if (
            quick_button
            and _ui_hook_enabled(settings, extension_id, "quick_button_enabled")
            and not _quick_button_superseded(extension_id)
        ):
            item: dict[str, Any] = {
                "extension_id": extension_id,
                "extension_name": extension_name,
                "label": quick_button.get("label", ""),
                "action": quick_button.get("action") or {},
            }
            if quick_button.get("icon"):
                item["icon"] = quick_button["icon"]
            quick_buttons.append(item)

        page = entrypoints.get("page") or {}
        if page and _ui_hook_enabled(settings, extension_id, "page_enabled"):
            page_item: dict[str, Any] = {
                "extension_id": extension_id,
                "extension_name": extension_name,
                "id": page.get("id", "main"),
                "label": page.get("label", ""),
                "open": page.get("open") or {},
            }
            if page.get("icon"):
                page_item["icon"] = page["icon"]
            if page.get("badge"):
                page_item["badge"] = page["badge"]
            pages.append(page_item)
    return _projection_cache_put("ui_hooks", key, {"quick_buttons": quick_buttons, "pages": pages})


def ui_hooks_cache_key() -> tuple[Any, ...]:
    return (store_fingerprint(), _file_fingerprint(_ui_settings_path()))


# ── extension settings + per-MCP-server enable/disable ───────────────
#
# User-configurable, manifest-declared settings (entrypoints.settings) plus a
# per-MCP-server enable/disable toggle. Non-secret values live in
# extension-settings.json; secret-typed values live ONLY in the OS keychain
# (via password_manager) and are never persisted to disk or returned by GET.

_EXT_SETTINGS_SCHEMA_VERSION = 1
_SETTING_SECRET_SERVICE = "better-agent-extension-setting"

# Free-text, user-authored "how to use this extension" instructions. Distinct
# from the author-shipped manifest instruction sections: this is the user's own
# preference text, injected into agent runs only while the extension is active.
_USER_INSTRUCTIONS_MAX_CHARS = 4_000


def _ext_settings_path() -> Path:
    return ba_home() / "extensions" / "extension-settings.json"


def extension_settings_fingerprint() -> tuple[int, int]:
    return _file_fingerprint(_ext_settings_path())


def _blank_ext_settings() -> dict[str, Any]:
    return {"schema_version": _EXT_SETTINGS_SCHEMA_VERSION, "extensions": {}}


def _load_ext_settings() -> dict[str, Any]:
    data = read_json(_ext_settings_path(), _blank_ext_settings())
    if data.get("schema_version") != _EXT_SETTINGS_SCHEMA_VERSION:
        raise ExtensionError(
            "Unsupported extension-settings schema; wipe extensions/extension-settings.json to start fresh"
        )
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        raise ExtensionError("Malformed extension-settings: extensions must be an object")
    return data


def _save_ext_settings(data: dict[str, Any]) -> None:
    write_json(_ext_settings_path(), data)


def _ext_settings_entry(data: dict[str, Any], extension_id: str) -> dict[str, Any]:
    entry = data["extensions"].get(extension_id)
    if not isinstance(entry, dict):
        entry = {}
        data["extensions"][extension_id] = entry
    if not isinstance(entry.get("values"), dict):
        entry["values"] = {}
    if not isinstance(entry.get("mcp_disabled"), list):
        entry["mcp_disabled"] = []
    if not isinstance(entry.get("frontend_modules_disabled"), list):
        entry["frontend_modules_disabled"] = []
    default_delivery = (
        _HARNESS_DELIVERY_RUNTIME
        if extension_id == MARKETPLACE_EXTENSION_ID
        else _HARNESS_DELIVERY_NATIVE
    )
    delivery = str(entry.get("harness_delivery") or default_delivery)
    if delivery not in _HARNESS_DELIVERY_MODES:
        delivery = _HARNESS_DELIVERY_NATIVE
    entry["harness_delivery"] = delivery
    return entry


def _setting_schema_list(extension_id: str) -> list[dict[str, Any]]:
    record = get_extension(extension_id)
    if not record:
        return []
    return list(record["manifest"].get("entrypoints", {}).get("settings") or [])


def _setting_secret_account(extension_id: str, key: str) -> str:
    return f"{extension_id}/{key}"


def get_extension_settings(extension_id: str) -> dict[str, Any]:
    """Schema + current values for Settings UI. Secrets are write-only:
    returned as ``None`` with a ``secret_present`` flag, never the value."""
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    schema = _setting_schema_list(extension_id)
    entry = _load_ext_settings()["extensions"].get(extension_id, {})
    stored_values = entry.get("values") if isinstance(entry, dict) else None
    stored_values = stored_values if isinstance(stored_values, dict) else {}
    values: dict[str, Any] = {}
    secret_present: dict[str, bool] = {}
    for item in schema:
        key = item["key"]
        if item["type"] == "secret":
            secret_present[key] = password_manager.has_service_password(
                _SETTING_SECRET_SERVICE, _setting_secret_account(extension_id, key)
            )
            values[key] = None
        else:
            values[key] = stored_values.get(key, item.get("default"))
    return {"schema": schema, "values": values, "secret_present": secret_present}


def set_extension_setting(extension_id: str, key: str, value: Any) -> dict[str, Any]:
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    spec = {item["key"]: item for item in _setting_schema_list(extension_id)}.get(key)
    if spec is None:
        raise ExtensionError(f"Unknown setting key: {key}")
    if spec["type"] == "secret":
        if not isinstance(value, str):
            raise ExtensionError(f"settings.{key} must be a string")
        account = _setting_secret_account(extension_id, key)
        if value:
            password_manager.store_service_password(
                {"service": _SETTING_SECRET_SERVICE, "account": account, "password": value}
            )
        else:
            password_manager.delete_service_password(
                {"service": _SETTING_SECRET_SERVICE, "account": account}
            )
        return get_extension_settings(extension_id)
    coerced = _coerce_setting_value(value, spec["type"], key, enum=spec.get("enum"))
    data = _load_ext_settings()
    _ext_settings_entry(data, extension_id)["values"][key] = coerced
    _save_ext_settings(data)
    return get_extension_settings(extension_id)


def resolve_all_settings(extension_id: str) -> dict[str, Any]:
    """All declared settings with values resolved — secrets read from the
    keychain. Used by the SDK loopback so an extension's MCP server reads its
    own config without secrets ever touching the environment."""
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    schema = _setting_schema_list(extension_id)
    data = _load_ext_settings()
    stored_values = data["extensions"].get(extension_id, {})
    stored_values = stored_values.get("values") if isinstance(stored_values, dict) else {}
    stored_values = stored_values if isinstance(stored_values, dict) else {}
    resolved: dict[str, Any] = {}
    for item in schema:
        key = item["key"]
        if item["type"] == "secret":
            try:
                resolved[key] = password_manager.get_service_password(
                    _SETTING_SECRET_SERVICE, _setting_secret_account(extension_id, key)
                )
            except Exception:
                resolved[key] = ""
        else:
            resolved[key] = stored_values.get(key, item.get("default"))
    return resolved


def is_mcp_server_enabled(extension_id: str, server_name: str) -> bool:
    entry = _load_ext_settings()["extensions"].get(extension_id, {})
    disabled = entry.get("mcp_disabled") if isinstance(entry, dict) else None
    if not isinstance(disabled, list):
        return True
    return server_name not in set(disabled)


def _frontend_module_key(slot: str, module_id: str) -> str:
    clean_slot = str(slot or "").strip()
    clean_id = str(module_id or "").strip()
    if not _ID_RE.fullmatch(clean_slot):
        raise ExtensionError("Invalid frontend module slot")
    if not _ID_RE.fullmatch(clean_id):
        raise ExtensionError("Invalid frontend module id")
    return f"{clean_slot}:{clean_id}"


def _extension_frontend_module_items(record: dict[str, Any]) -> list[dict[str, str]]:
    return list((record.get("manifest") or {}).get("entrypoints", {}).get("frontend_modules") or [])


def _frontend_module_exists(record: dict[str, Any], slot: str, module_id: str) -> bool:
    return any(item["slot"] == slot and item["id"] == module_id for item in _extension_frontend_module_items(record))


def is_frontend_module_enabled(extension_id: str, slot: str, module_id: str) -> bool:
    key = _frontend_module_key(slot, module_id)
    entry = _load_ext_settings()["extensions"].get(extension_id, {})
    disabled = entry.get("frontend_modules_disabled") if isinstance(entry, dict) else None
    if not isinstance(disabled, list):
        return True
    return key not in set(str(item) for item in disabled)


def set_frontend_module_enabled(extension_id: str, slot: str, module_id: str, enabled: bool) -> bool:
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    key = _frontend_module_key(slot, module_id)
    if not _frontend_module_exists(record, slot, module_id):
        raise ExtensionError("Frontend module not declared by extension")
    data = _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    disabled = set(str(item) for item in entry.get("frontend_modules_disabled") or [])
    if enabled:
        disabled.discard(key)
    else:
        disabled.add(key)
    entry["frontend_modules_disabled"] = sorted(disabled)
    _save_ext_settings(data)
    return key not in entry["frontend_modules_disabled"]


def extension_frontend_modules(extension_id: str) -> list[dict[str, Any]]:
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    frontend_modules = _extension_frontend_module_items(record)
    frontend_path = str(entrypoints.get("frontend") or "")
    runtime_root = runtime_package_root_for_record(record)
    loadable = bool(frontend_path and runtime_root is not None and _record_active(record) and _record_runtime_ready(record))
    bust = ""
    if loadable:
        frontend_assets = [frontend_path, *[str(item.get("module") or "") for item in frontend_modules]]
        bust = f"?v={_frontend_asset_version(record, runtime_root, frontend_assets)}"
    result: list[dict[str, Any]] = []
    for item in frontend_modules:
        module_path = str(item.get("module") or "")
        enabled = is_frontend_module_enabled(extension_id, item["slot"], item["id"])
        module_url = (
            f"/api/extensions/{manifest['id']}/frontend/{module_path}{bust}"
            if loadable and enabled and module_path
            else ""
        )
        result.append({
            "slot": item["slot"],
            "id": item["id"],
            "label": item["label"],
            "kind": item["kind"],
            "module": module_path,
            "module_url": module_url,
            "enabled": enabled,
            "loadable": loadable,
        })
    return result


def get_user_instructions(extension_id: str) -> str:
    """The user's free-text "how to use this extension" preferences.

    Empty string when never set. This is the user's own guidance, separate
    from the extension author's manifest instruction sections.
    """
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    entry = _load_ext_settings()["extensions"].get(extension_id, {})
    raw = entry.get("user_instructions") if isinstance(entry, dict) else ""
    return raw if isinstance(raw, str) else ""


def set_user_instructions(extension_id: str, text: Any) -> str:
    """Store the user's per-extension instruction text. Trims surrounding
    whitespace; an empty result clears it. Capped server-side."""
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ExtensionError("user_instructions must be a string")
    cleaned = text.strip()
    if len(cleaned) > _USER_INSTRUCTIONS_MAX_CHARS:
        raise ExtensionError(
            f"user_instructions is too long (max {_USER_INSTRUCTIONS_MAX_CHARS} characters)"
        )
    data = _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    if cleaned:
        entry["user_instructions"] = cleaned
    else:
        entry.pop("user_instructions", None)
    _save_ext_settings(data)
    return cleaned


def user_instruction_contexts(*, bare_config: bool = False) -> list[dict[str, Any]]:
    """Capability-context block carrying the user's per-extension instructions.

    Only active (enabled + runtime-ready) extensions with non-empty user
    instructions contribute. Returns the provider-uniform capability-context
    shape consumed by every runner, so the same text reaches Claude, Codex, and
    Gemini identically and is re-read fresh each turn (restart-tolerant).
    """
    if bare_config:
        return []
    settings = _load_ext_settings()["extensions"]
    blocks: list[str] = []
    for record in list_extensions():
        if not _record_active(record) or not _record_runtime_ready(record):
            continue
        manifest = record.get("manifest") or {}
        extension_id = str(manifest.get("id") or "")
        if not extension_id:
            continue
        entry = settings.get(extension_id)
        raw = entry.get("user_instructions") if isinstance(entry, dict) else ""
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            continue
        name = str(manifest.get("name") or extension_id)
        blocks.append(f"### {name} ({extension_id})\n{text}")
    if not blocks:
        return []
    content = (
        "Your personal instructions for how to use specific extensions. Follow "
        "them whenever you use the matching extension's tools or features.\n\n"
        + "\n\n".join(blocks)
    )
    return [{
        "name": "Extension Instructions",
        "category": "instructions",
        "content_kind": "extension_user_instructions",
        "content": content,
    }]


def harness_delivery_mode(extension_id: str, *, settings: dict[str, Any] | None = None) -> str:
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    data = settings if settings is not None else _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    return str(entry["harness_delivery"])


def set_harness_delivery_mode(extension_id: str, mode: str) -> str:
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    clean = str(mode or "").strip()
    if clean not in _HARNESS_DELIVERY_MODES:
        raise ExtensionError(f"harness_delivery must be one of: {', '.join(sorted(_HARNESS_DELIVERY_MODES))}")
    data = _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    entry["harness_delivery"] = clean
    _save_ext_settings(data)
    reconcile_runtime_skills()
    reconcile_native_mcp_servers()
    return clean


def set_mcp_server_enabled(extension_id: str, server_name: str, enabled: bool) -> bool:
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    if not _ID_RE.fullmatch(server_name):
        raise ExtensionError("Invalid MCP server name")
    data = _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    disabled = set(entry.get("mcp_disabled") or [])
    if enabled:
        disabled.discard(server_name)
    else:
        disabled.add(server_name)
    entry["mcp_disabled"] = sorted(disabled)
    _save_ext_settings(data)
    return server_name not in entry["mcp_disabled"]


def extension_config(extension_id: str) -> dict[str, Any]:
    """Full per-extension config bundle for the Settings panel: UI-surface
    toggles, MCP servers with enabled state, and declared settings (secrets
    write-only)."""
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    entrypoints = record["manifest"].get("entrypoints", {})
    return {
        "id": extension_id,
        "name": record["manifest"].get("name") or extension_id,
        "required": extension_id in REQUIRED_EXTENSION_IDS,
        "has_quick_button": bool(entrypoints.get("quick_button")),
        "has_page": bool(entrypoints.get("page")),
        "harness_delivery": harness_delivery_mode(extension_id),
        "harness_additions": extension_harness_additions(record),
        "internal_llm_tasks": extension_internal_llm_tasks(record),
        "user_instructions": get_user_instructions(extension_id),
        "ui": get_ui_settings(extension_id),
        "frontend_modules": extension_frontend_modules(extension_id),
        "mcp": extension_mcp_servers(extension_id),
        "remote_services": list(entrypoints.get("remote_services") or []),
        "settings": get_extension_settings(extension_id),
        "permissions": {
            "declared": declared_permissions(record),
            "optional": optional_permissions(record),
            "grants": permission_grants(record),
            "effective": effective_permissions(record),
        },
    }


def extension_internal_llm_tasks(record: dict[str, Any]) -> list[str]:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    return list(_EXTENSION_SETTINGS_INTERNAL_LLM_TASKS.get(extension_id, ()))


def extension_provisioned_internal_llm_tasks(record: dict[str, Any]) -> list[str]:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    return list(_BUILTIN_INTERNAL_LLM_TASKS.get(extension_id, ()))


def all_internal_llm_task_keys() -> list[str]:
    """Every internal-LLM task key contributed by builtin extensions (public
    and private-registry), in stable declaration order. Absent private
    checkout ⇒ private tasks are simply not contributed."""
    keys: list[str] = []
    for task_keys in _BUILTIN_INTERNAL_LLM_TASKS.values():
        for key in task_keys:
            if key not in keys:
                keys.append(key)
    return keys


def internal_llm_task_labels() -> dict[str, str]:
    """Display labels for extension-contributed tasks that have no public
    i18n entry (private-registry tasks). Public builtin tasks are labeled
    via frontend i18n and are absent here."""
    return {
        str(k): str(v)
        for k, v in (_PRIVATE_REGISTRY.get("llm_task_labels") or {}).items()
        if str(k) and str(v)
    }


def extension_internal_llm_task_keys() -> set[str]:
    task_keys: set[str] = set()
    for keys in _EXTENSION_SETTINGS_INTERNAL_LLM_TASKS.values():
        task_keys.update(keys)
    return task_keys


def extension_harness_additions(record: dict[str, Any]) -> list[dict[str, str]]:
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    additions: list[dict[str, str]] = []
    for item in extension_instructions.instruction_items_from_entrypoints(entrypoints) or []:
        if isinstance(item, dict) and item.get("name"):
            additions.append({
                "kind": "instructions",
                "name": str(item["name"]),
                "detail": "project" if item.get("level") == "project" else "global",
            })
    for item in entrypoints.get("skills") or []:
        if isinstance(item, dict) and item.get("name"):
            additions.append({"kind": "skill", "name": str(item["name"]), "detail": ""})
    for item in _stored_mcp_entrypoints(record):
        name = str(item.get("name") or "")
        if not name or name in _RESERVED_MCP_SERVER_NAMES:
            continue
        additions.append({
            "kind": "mcp",
            "name": name,
            "detail": "enabled" if is_mcp_server_enabled(str(manifest.get("id") or ""), name) else "disabled",
        })
    return additions


def extension_mcp_servers(extension_id: str) -> list[dict[str, Any]]:
    """MCP servers an extension provides, with current enabled state — for the
    Settings UI."""
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    record = get_extension(extension_id)
    servers: list[dict[str, Any]] = []
    for item in _stored_mcp_entrypoints(record):
        if item["name"] in _RESERVED_MCP_SERVER_NAMES:
            continue
        servers.append(
            {
                "name": item["name"],
                "label": item["name"],
                "user_facing": item.get("user_facing", True),
                "enabled": is_mcp_server_enabled(extension_id, item["name"]),
            }
        )
    return servers
