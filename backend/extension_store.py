from __future__ import annotations

import copy
from contextlib import contextmanager
import gzip
import io
import logging
import zlib
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable
from urllib.parse import quote, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from env_compat import dual_env_many, get_env
from json_store import read_json, write_json
from paths import ba_home
import password_manager
import extension_applied_config
import extension_descriptions
import provider_kinds
import extension_instructions
import extension_mcp
import native_mcp_grants
import installation_profile
from bundled_extensions import PUBLIC_EXTENSION_PATHS
import harness_run_projection
import dependency_plan

logger = logging.getLogger(__name__)

STORE_SCHEMA_VERSION = 2
MANIFEST_KIND = "better-agent-extension"
# Slow-call floor for backend routes that declare no `entrypoints.backend_timeouts`
# budget. A route that declares one is judged against that declaration instead
# (see `record_slow_backend_call`).
EXTENSION_SLOW_CALL_SECONDS = 2.0
_EXTENSION_SLOW_CALL_LIMIT = 3
_EXTENSION_SLOW_CALL_WINDOW_SECONDS = 10 * 60.0
_EXTENSION_INCIDENT_FUTURE_SKEW_SECONDS = 60.0
_EXTENSION_INCIDENT_DEDUP_SECONDS = 20 * 60.0
_EXTENSION_INCIDENT_IDS_PER_NODE = 2048

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]{0,127}$")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_GIT_SCP_RE = re.compile(r"^git@[A-Za-z0-9_.-]+:[A-Za-z0-9_.~/-]+\.git$")
_ALLOWED_SURFACES = {"backend_feature", "frontend_feature", "runtime_mcp", "instructions", "skills", "agents", "daemons"}
# Providers an extension-declared subagent can target. Each maps to a native
# agent-definition surface; providers without one (e.g. gemini/agy) get a
# graceful no-op at materialization time.
_AGENT_TARGET_PROVIDERS = frozenset({"claude", "codex"})
# Daemon lifecycles: "backend" daemons live and die with the backend process;
# "supervisor" daemons are installed copies run by the platform daemon host and
# survive backend restarts (they auto-update from the active checkout, so they
# require the stronger consent level).
_DAEMON_LIFECYCLES = {"backend", "supervisor"}
_DAEMON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_DAEMON_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,79}$")
# Ports the platform itself binds; a daemon declaring one of these is refused
# at validation time (a rogue bind beyond the declaration is the same trust
# level as any extension code — the declaration is the contract, not a sandbox).
_DAEMON_RESERVED_PORTS = frozenset({8000, 8002, 5173, 18765})
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
_NATIVE_HARNESS_KINDS = frozenset({"instructions", "skill", "mcp"})
_PROJECTION_CACHE: dict[tuple[str, tuple[Any, ...]], Any] = {}
_RUNTIME_READY_PROJECTION: dict[str, bool] = {}
_RUNTIME_PACKAGE_FINGERPRINTS: dict[str, str] = {}
_RUNTIME_READY_PROJECTION_LOCK = threading.Lock()
_RUNTIME_AGENT_ENTRIES_CACHE: tuple[
    StoreFingerprint,
    tuple[dict[str, str], ...],
] | None = None
_RUNTIME_AGENT_ENTRIES_LOCK = threading.Lock()
StoreFingerprint = tuple[str, str]
_ENABLED_CACHE: dict[str, tuple[StoreFingerprint, bool]] = {}
_ENABLED_CACHE_LOCK = threading.Lock()
# Fingerprint-keyed cache for get_extension() — defined here (beside the
# other store caches) so _clear_projection_cache can reference it.
_GET_EXTENSION_CACHE: dict[str, tuple[StoreFingerprint, dict[str, Any] | None]] = {}
_GET_EXTENSION_CACHE_LOCK = threading.Lock()
_BUILTIN_FEATURE_CACHE: dict[str, tuple[tuple[Any, ...], bool]] = {}
_BUILTIN_FEATURE_CACHE_LOCK = threading.Lock()
_STORE_FINGERPRINT_CACHE: tuple[float, StoreFingerprint] | None = None
_STORE_FINGERPRINT_FILE_STATE: tuple[str, int, int, int, int] | None = None
_STORE_FINGERPRINT_CACHE_LOCK = threading.Lock()
_STORE_FINGERPRINT_TTL_SECONDS = 0.5
_STORE_MUTATION_LOCAL = threading.local()
_STORE_MUTATION_SUBSCRIBERS: dict[str, Callable[[], None]] = {}
_STORE_MUTATION_SUBSCRIBERS_LOCK = threading.Lock()
_RECONCILED_STORE_FINGERPRINT: tuple[str, StoreFingerprint] | None = None
_RECONCILED_STORE_LOCK = threading.Lock()
_CORE_ROLE_OWNERS_CACHE: tuple[StoreFingerprint, MappingProxyType] | None = None
_CORE_ROLE_OWNERS_LOCK = threading.Lock()
_PACKAGE_PUBLISH_LOCKS: dict[str, threading.Lock] = {}
_PACKAGE_PUBLISH_LOCK_GUARD = threading.Lock()
_EXT_SETTINGS_LOCK = threading.RLock()
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
    "better-agent-coordination",
    "session-bridge",
}

CORE_ROLES = frozenset({
    "adv", "agent-board", "assistant", "auto-tagging", "browser-harness", "canvas",
    "composer-fill",
    "credential-broker", "machine-nodes", "project-structure",
    "prompt-engineer", "requirements", "routines", "scheduler",
    "supervisor", "team-orchestration", "testape",
})


# Public builtin ids stay literal in the public repo.
BUILTIN_ASK_EXTENSION_ID = "ofek-dev.ask"
BUILTIN_SESSION_BRIDGE_EXTENSION_ID = "ofek-dev.session-bridge"
BUILTIN_SESSION_CONTROL_EXTENSION_ID = "ofek-dev.session-control"
BUILTIN_COORDINATION_EXTENSION_ID = "ofek-dev.coordination"
# REMOVED: Provider Config Sync builtin extension (replaced by temporal harness profiles)
BUILTIN_TODOS_EXTENSION_ID = "ofek-dev.todos"
BUILTIN_FILE_EDIT_EXTENSION_ID = "ofek-dev.file-edit"
BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID = "better-agent.harness-for-better-agent"
BUILTIN_USER_ATTENTION_EXTENSION_ID = "ofek-dev.user-attention"
BUILTIN_SWITCH_CONTROL_EXTENSION_ID = "ofek-dev.switch-control"
BUILTIN_AUTO_TAGGING_EXTENSION_ID = "ofek-dev.auto-tagging"
_BUILTIN_MCP_REPLACEMENTS_BY_EXTENSION_ID = {
    BUILTIN_COORDINATION_EXTENSION_ID: frozenset({"better-agent-coordination"}),
}
_MCP_REPLACEMENT_CORE_ROLES = {
    "project-updates": "project-structure",
    "get-requirements": "requirements",
    "credential-broker": "credential-broker",
}
MARKETPLACE_EXTENSION_ID = "ofek-dev.marketplace"
_BROKERED_MCP_EXTENSION_IDS = frozenset({
    BUILTIN_COORDINATION_EXTENSION_ID,
    BUILTIN_SESSION_BRIDGE_EXTENSION_ID,
    BUILTIN_SESSION_CONTROL_EXTENSION_ID,
    MARKETPLACE_EXTENSION_ID,
})
REQUIRED_EXTENSION_IDS = {MARKETPLACE_EXTENSION_ID}
PUBLIC_EXTENSION_LIST_HIDDEN_IDS = frozenset()
_OBSOLETE_EXTENSION_IDS = {
    "better-agent.marketplace": MARKETPLACE_EXTENSION_ID,
    "ofek-dev.needs-user-decision": BUILTIN_USER_ATTENTION_EXTENSION_ID,
}
_PUBLIC_EXTENSION_PATHS = PUBLIC_EXTENSION_PATHS
_EXTENSION_DISPLAY_NAMES = {
    BUILTIN_ASK_EXTENSION_ID: "Ask",
    BUILTIN_SESSION_BRIDGE_EXTENSION_ID: "Session Bridge",
    BUILTIN_SESSION_CONTROL_EXTENSION_ID: "Session Control",
    BUILTIN_COORDINATION_EXTENSION_ID: "Coordination",
    BUILTIN_TODOS_EXTENSION_ID: "Todos",
    BUILTIN_FILE_EDIT_EXTENSION_ID: "File Edit",
    BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: "Harness instructions",
    BUILTIN_USER_ATTENTION_EXTENSION_ID: "User attention",
    BUILTIN_SWITCH_CONTROL_EXTENSION_ID: "Line Switch",
    MARKETPLACE_EXTENSION_ID: "Marketplace",
}
_DEFAULT_MARKETPLACE_BASE_URL = "https://singular-labs.ai/api/marketplace"
_DEFAULT_MARKETPLACE_PUBLIC_KEY = "a61a192e23f0f0898fa096ae64e0d22d853eb0701e2c94a6d55fff7b2f52b7fd"
_MARKETPLACE_USER_AGENT = "BetterAgentMarketplace/1.0"
_MARKETPLACE_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+:/#-]{0,119}$")
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
_SMOKE_IMPORT_OWNER_DEADLINE_SECONDS = 60
_MARKETPLACE_PREVIEW_TTL_SECONDS = 5 * 60.0
_MAX_MARKETPLACE_PREVIEWS = 256
_MARKETPLACE_PREVIEWS: dict[str, tuple[float, str, dict[str, Any]]] = {}
_MARKETPLACE_PREVIEWS_LOCK = threading.Lock()
_required_artifact_update_checked: set[str] = set()

_BUILTIN_INTERNAL_LLM_TASKS: dict[str, tuple[str, ...]] = {
    BUILTIN_ASK_EXTENSION_ID: ("session_search_worker",),
    BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: ("extension_context_audit",),
}
_DEFAULT_NATIVE_HARNESS_BY_EXTENSION_ID: dict[str, tuple[str, ...]] = {
    BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID: (
        "instructions:better-agent-harness-behavior",
    ),
}
_EXTENSION_SETTINGS_INTERNAL_LLM_TASKS: dict[str, tuple[str, ...]] = {
    **(
        {BUILTIN_ASK_EXTENSION_ID: ("session_search_worker",)}
        if BUILTIN_ASK_EXTENSION_ID
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
}
_CORE_ROLE_INTERNAL_LLM_TASKS: dict[str, tuple[str, ...]] = {
    "requirements": ("requirement_analysis",),
    "team-orchestration": (
        "delegation_task",
        "delegation_message",
        "delegation_ask",
    ),
}
_BUILTIN_RUNTIME_REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
}

_PUBLIC_FRONTEND_BUILTIN_KEYS = {
    "ask": BUILTIN_ASK_EXTENSION_ID,
    "sessionBridge": BUILTIN_SESSION_BRIDGE_EXTENSION_ID,
}

_ROLE_FRONTEND_KEYS = {
    "team": "team-orchestration", "supervisor": "supervisor",
    "projectStructure": "project-structure", "machineNodes": "machine-nodes",
    "credentialBroker": "credential-broker", "canvas": "canvas",
    "promptEngineer": "prompt-engineer", "browserHarness": "browser-harness",
    "agentBoard": "agent-board", "requirements": "requirements",
    "testape": "testape", "scheduler": "scheduler", "routines": "routines",
    "assistant": "assistant",
}


def builtin_extension_id_map() -> dict[str, str]:
    resolved = dict(_PUBLIC_FRONTEND_BUILTIN_KEYS)
    for key, role in _ROLE_FRONTEND_KEYS.items():
        extension_id = extension_id_for_role(role)
        if extension_id:
            resolved[key] = extension_id
    return resolved


class ExtensionError(ValueError):
    pass


class ExtensionConsentRequired(ExtensionError):
    """Raised when a non-builtin extension is enabled before the user has
    consented to its declared permission set (trusted-by-install model)."""
    pass


_STORE_PATH: tuple[str, Path] | None = None


def _store_path() -> Path:
    global _STORE_PATH
    home = ba_home()
    home_key = str(home)
    if _STORE_PATH is None or _STORE_PATH[0] != home_key:
        _STORE_PATH = (home_key, home / "extensions" / "extensions.json")
    return _STORE_PATH[1]


def _slow_calls_path() -> Path:
    return ba_home() / "extensions" / "slow-backend-calls.json"


def _clear_slow_call_history(extension_id: str) -> None:
    with _store_lock():
        history = read_json(_slow_calls_path(), {"extensions": {}})
        histories = history.get("extensions")
        if not isinstance(histories, dict) or extension_id not in histories:
            return
        histories.pop(extension_id, None)
        write_json(_slow_calls_path(), history)


def _rotate_activation_identity(record: dict[str, Any]) -> str:
    activation_id = uuid.uuid4().hex
    record["activation_id"] = activation_id
    return activation_id


def activation_identity(extension_id: str) -> str:
    record = _load()["extensions"].get(extension_id)
    if not isinstance(record, dict) or record.get("enabled") is not True:
        return ""
    activation_id = record.get("activation_id")
    return activation_id if isinstance(activation_id, str) and re.fullmatch(r"[0-9a-f]{32}", activation_id) else ""


def _fingerprint_store_file(
    path: Path,
) -> tuple[StoreFingerprint, tuple[str, int, int, int, int]]:
    path_key = str(path)
    try:
        with path.open("rb") as handle:
            content = handle.read()
            stat = os.fstat(handle.fileno())
    except FileNotFoundError:
        return (path_key, ""), (path_key, 0, 0, 0, 0)
    return (
        (path_key, hashlib.sha256(content).hexdigest()),
        (path_key, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size),
    )


def store_fingerprint() -> StoreFingerprint:
    global _STORE_FINGERPRINT_CACHE, _STORE_FINGERPRINT_FILE_STATE
    now = time.monotonic()
    path = _store_path()
    current_path = str(path)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        cached = _STORE_FINGERPRINT_CACHE
        if (
            cached is not None
            and now - cached[0] <= _STORE_FINGERPRINT_TTL_SECONDS
            and cached[1][0] == current_path
        ):
            return cached[1]
    try:
        stat = path.stat()
        file_state = (
            current_path,
            stat.st_dev,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_size,
        )
    except FileNotFoundError:
        file_state = (current_path, 0, 0, 0, 0)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        cached = _STORE_FINGERPRINT_CACHE
        if (
            cached is not None
            and cached[1][0] == current_path
            and _STORE_FINGERPRINT_FILE_STATE == file_state
        ):
            _STORE_FINGERPRINT_CACHE = (now, cached[1])
            return cached[1]
    fingerprint, file_state = _fingerprint_store_file(path)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        _STORE_FINGERPRINT_CACHE = (now, fingerprint)
        _STORE_FINGERPRINT_FILE_STATE = file_state
    return fingerprint


def _refresh_store_fingerprint_cache(path: Path | None = None) -> StoreFingerprint:
    global _STORE_FINGERPRINT_CACHE, _STORE_FINGERPRINT_FILE_STATE
    path = path or _store_path()
    fingerprint, file_state = _fingerprint_store_file(path)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        _STORE_FINGERPRINT_CACHE = (time.monotonic(), fingerprint)
        _STORE_FINGERPRINT_FILE_STATE = file_state
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
    global _RECONCILED_STORE_FINGERPRINT, _CORE_ROLE_OWNERS_CACHE
    global _RUNTIME_AGENT_ENTRIES_CACHE
    _PROJECTION_CACHE.clear()
    with _RUNTIME_READY_PROJECTION_LOCK:
        _RUNTIME_READY_PROJECTION.clear()
        _RUNTIME_PACKAGE_FINGERPRINTS.clear()
    with _RECONCILED_STORE_LOCK:
        _RECONCILED_STORE_FINGERPRINT = None
    # get_extension's fingerprint cache auto-invalidates on any store write
    # (file mtime/size changes), but a same-fingerprint forced refresh must
    # drop it too so a reconcile that rewrites identical bytes is observed.
    with _GET_EXTENSION_CACHE_LOCK:
        _GET_EXTENSION_CACHE.clear()
    with _CORE_ROLE_OWNERS_LOCK:
        _CORE_ROLE_OWNERS_CACHE = None
    with _RUNTIME_AGENT_ENTRIES_LOCK:
        _RUNTIME_AGENT_ENTRIES_CACHE = None


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
    depth = int(getattr(_STORE_MUTATION_LOCAL, "depth", 0))
    _STORE_MUTATION_LOCAL.depth = depth + 1
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
            _STORE_MUTATION_LOCAL.depth = depth
            if depth == 0 and bool(getattr(_STORE_MUTATION_LOCAL, "pending", False)):
                _STORE_MUTATION_LOCAL.pending = False
                _notify_store_mutated()


def subscribe_store_mutations(name: str, callback: Callable[[], None]) -> None:
    with _STORE_MUTATION_SUBSCRIBERS_LOCK:
        _STORE_MUTATION_SUBSCRIBERS[name] = callback


def unsubscribe_store_mutations(name: str) -> None:
    with _STORE_MUTATION_SUBSCRIBERS_LOCK:
        _STORE_MUTATION_SUBSCRIBERS.pop(name, None)


def _notify_store_mutated() -> None:
    with _STORE_MUTATION_SUBSCRIBERS_LOCK:
        subscribers = list(_STORE_MUTATION_SUBSCRIBERS.items())
    for name, callback in subscribers:
        try:
            callback()
        except Exception:
            logger.exception("extension store mutation subscriber failed: %s", name)


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
    if _annotate_legacy_quarantine_cohorts(data):
        _write_store_unlocked(data)
    return data


def _annotate_legacy_quarantine_cohorts(data: dict[str, Any]) -> bool:
    """Normalize only legacy auto-quarantines whose cohort is unambiguous."""
    records = data.get("extensions") or {}
    changed = False
    for trigger_id, trigger in sorted(records.items()):
        if not isinstance(trigger, dict) or trigger_id in REQUIRED_EXTENSION_IDS:
            continue
        quarantine = trigger.get("quarantine") or {}
        if (
            trigger.get("enabled") is not False
            or quarantine.get("reason") not in {"repeated_slow_backend_calls", "repeated_backend_timeouts"}
            or quarantine.get("attributed_extension_id") != trigger_id
            or quarantine.get("attributed_generation")
            or quarantine.get("cohort") is not None
            or not isinstance(quarantine.get("at"), str)
            or not quarantine["at"]
        ):
            continue
        generation = _record_generation(trigger)
        if not generation:
            continue
        signature = (quarantine["reason"], trigger_id, quarantine["at"])
        matching: set[str] = set()
        valid = True
        manifests: dict[str, dict[str, Any]] = {}
        for extension_id, candidate in records.items():
            if not isinstance(candidate, dict):
                continue
            candidate_quarantine = candidate.get("quarantine") or {}
            candidate_signature = (
                candidate_quarantine.get("reason"),
                candidate_quarantine.get("attributed_extension_id"),
                candidate_quarantine.get("at"),
            )
            if (
                candidate_quarantine.get("reason") == signature[0]
                and candidate_quarantine.get("attributed_extension_id") == trigger_id
                and candidate_signature != signature
            ):
                valid = False
                break
            if candidate_signature != signature:
                continue
            if (
                extension_id in REQUIRED_EXTENSION_IDS
                or candidate.get("enabled") is not False
                or candidate_quarantine.get("attributed_generation")
                or candidate_quarantine.get("cohort") is not None
            ):
                valid = False
                break
            try:
                stored_manifest = copy.deepcopy(candidate.get("manifest") or {})
                stored_entrypoints = stored_manifest.get("entrypoints") or {}
                for key in list(stored_entrypoints):
                    if stored_entrypoints[key] is None:
                        stored_entrypoints.pop(key)
                for optional_surface in ("quick_button", "page"):
                    surface = stored_entrypoints.get(optional_surface)
                    if isinstance(surface, dict) and not surface.get("label"):
                        stored_entrypoints.pop(optional_surface, None)
                manifest = validate_manifest(stored_manifest)
            except ExtensionError:
                valid = False
                break
            if manifest["id"] != extension_id:
                valid = False
                break
            matching.add(extension_id)
            manifests[extension_id] = manifest
        if not valid or trigger_id not in matching:
            continue
        closure = {trigger_id}
        while True:
            dependents = {
                extension_id
                for extension_id, manifest in manifests.items()
                if set(manifest.get("dependencies") or ()).intersection(closure)
            }
            expanded = closure | dependents
            if expanded == closure:
                break
            closure = expanded
        if closure != matching:
            continue
        pending = set(closure)
        while pending:
            ready = {
                extension_id for extension_id in pending
                if not set(manifests[extension_id].get("dependencies") or ()).intersection(pending)
            }
            if not ready:
                valid = False
                break
            pending -= ready
        if not valid:
            continue
        cohort = sorted(closure)
        for extension_id in cohort:
            candidate_quarantine = records[extension_id]["quarantine"]
            candidate_quarantine["attributed_generation"] = generation
            candidate_quarantine["cohort"] = cohort
        changed = True
    return changed


def _write_store_unlocked(data: dict[str, Any]) -> None:
    owners: dict[str, str] = {}
    for extension_id, record in (data.get("extensions") or {}).items():
        if not isinstance(record, dict) or record.get("enabled") is not True:
            continue
        for role in ((record.get("manifest") or {}).get("core_roles") or []):
            owner = owners.get(role)
            if owner and owner != extension_id:
                raise ExtensionError(f"core role {role!r} is declared by multiple active extensions")
            owners[role] = extension_id
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
    _STORE_MUTATION_LOCAL.pending = True


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
    managed_ids = set(_PUBLIC_EXTENSION_PATHS)
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
    while True:
        with _store_lock():
            data = _read_store_unlocked()
            previous_data = copy.deepcopy(data)
            base_fingerprint = _refresh_store_fingerprint_cache()
        changed, public_changed, recovered = _reconcile_loaded_store(data)
        if not changed:
            return data, changed, public_changed
        with _store_lock():
            if _refresh_store_fingerprint_cache() != base_fingerprint:
                continue
            _write_store_unlocked(data)
        try:
            _reconcile_recovered_cohorts(data, recovered)
        except Exception:
            with _store_lock():
                _write_store_unlocked(previous_data)
            for extension_id in recovered:
                _evict_extension_backend(extension_id)
            raise
        return data, changed, public_changed


# Each managed package revision produces a
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
        # A version dir can be removed by a concurrent install/GC between
        # iterdir() above and stat() here; treat a vanished path as oldest so
        # it sorts to the deletion tail instead of aborting the whole reconcile
        # (the docstring promises we "fail open per-dir").
        def _mtime_or_floor(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        fallbacks.sort(key=_mtime_or_floor, reverse=True)
        # Drop entries that disappeared so we don't schedule them for rmtree;
        # rmtree(ignore_errors=True) would tolerate them too, but a tidy list
        # makes the post-condition in tests unambiguous.
        fallbacks = [p for p in fallbacks if p.exists()]
        for stale in fallbacks[_MAX_FALLBACK_VERSIONS:]:
            shutil.rmtree(stale, ignore_errors=True)


def _reconcile_loaded_store(data: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    changed = False
    public_changed = False
    for record in (data.get("extensions") or {}).values():
        if isinstance(record, dict) and not (
            isinstance(record.get("activation_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", record["activation_id"])
        ):
            _rotate_activation_identity(record)
            changed = True
    if data.pop("builtin_extensions_seeded", None) is not None:
        changed = True
    if _purge_obsolete_extension_records(data):
        changed = True
    if _rehydrate_installed_extension_records(data):
        changed = True
    if _ensure_public_extensions(data):
        changed = True
        public_changed = True
    local_changed, recovered = _ensure_local_extensions(data)
    if local_changed:
        changed = True
    if recovered:
        public_changed = True
    if _ensure_installed_smoke_current(data):
        changed = True
    _prune_extension_versions(data)
    return changed, public_changed, recovered


def _ensure_installed_smoke_current(data: dict[str, Any]) -> bool:
    """Re-smoke installed extensions whose stored result is a stale "passed".

    A stored smoke is stale when the manifest's ``required_paths`` /
    ``python_modules`` changed since it was recorded but the install is still
    present. Local/path extensions are refreshed by ``_ensure_local_extensions``
    on manifest change; marketplace and other remotely-installed extensions are
    not, so a manifest smoke-path update left them permanently
    not-runtime-ready — the stale record never re-ran, silently dropping the
    extension (and its MCP) from every session. Re-smoke those here so a valid
    install self-heals on reconcile. Genuinely-failed smokes are left as-is;
    only a previously-passing install is re-verified, and a re-run that fails
    leaves the record unchanged (still not-ready)."""
    changed = False
    for record in (data.get("extensions") or {}).values():
        if not isinstance(record, dict):
            continue
        manifest = record.get("manifest") or {}
        if "protocol" not in manifest:
            continue
        smoke = record.get("smoke_test")
        if not isinstance(smoke, dict) or smoke.get("status") != "passed":
            continue
        if _record_smoke_test_current(record):
            continue
        install_path = Path(
            str((record.get("source") or {}).get("install_path") or "")
        ).expanduser()
        if not install_path.is_dir():
            continue
        try:
            fresh = _run_extension_smoke_test(manifest, install_path)
        except Exception:
            logger.exception(
                "stale smoke re-run failed for %s", manifest.get("id")
            )
            continue
        record["smoke_test"] = fresh
        record["updated_at"] = _now()
        changed = True
    return changed


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


def _reject_legacy_authority_replacement(
    current_record: dict[str, Any] | None,
    incoming_manifest: dict[str, Any],
) -> None:
    if not isinstance(current_record, dict):
        return
    current_manifest = current_record.get("manifest")
    if not isinstance(current_manifest, dict):
        return
    current_permissions = current_manifest.get("permissions") or {}
    incoming_permissions = incoming_manifest.get("permissions") or {}
    current_scoped = bool(
        current_permissions.get("capabilities")
        or current_manifest.get("core_roles")
    )
    incoming_scoped = bool(
        incoming_permissions.get("capabilities")
        or incoming_manifest.get("core_roles")
    )
    if (
        current_scoped
        and not incoming_scoped
        and incoming_permissions.get("internal_loopback") is True
    ):
        raise ExtensionError(
            "sync artifact cannot replace scoped authority with internal_loopback"
        )


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
    # Packages that could not be shipped. Reported in the payload so the node
    # (and the UI reading its result) sees a partial sync as partial instead of
    # believing it received every extension.
    artifact_failures: list[dict[str, str]] = []
    for extension_id, record in (data.get("extensions") or {}).items():
        if not isinstance(record, dict):
            continue
        source = record.get("source") or {}
        raw_install_path = str(source.get("install_path") or "")
        install_path = Path(raw_install_path)
        # Install paths are absolute under the extensions root. A relative one
        # is a corrupt record that would resolve against the process cwd and
        # try to ship that whole tree.
        if not install_path.is_absolute() or not install_path.is_dir():
            if raw_install_path:
                logger.warning(
                    "extension %s has unusable install_path %r; skipping package",
                    extension_id,
                    raw_install_path,
                )
                artifact_failures.append({
                    "extension_id": extension_id,
                    "error": f"unusable install_path {raw_install_path!r}",
                })
            continue
        try:
            archive = _build_package_artifact(install_path)
        except Exception as exc:
            # One unshippable package must not deny every node all the others.
            # The next store change or node reconnect retries it.
            logger.exception("extension %s package export failed; skipping", extension_id)
            artifact_failures.append({"extension_id": extension_id, "error": str(exc)})
            continue
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
        "artifact_failures": artifact_failures,
    }


def _install_synced_artifact(
    artifact: dict[str, Any],
    records: dict[str, Any],
    current_records: dict[str, Any],
) -> None:
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
    current_source = record.get("source")
    source = copy.deepcopy(current_source) if isinstance(current_source, dict) else {}
    target = _install_root() / extension_id / "versions" / actual_sha
    staging = target.parent / f".sync-staging-{uuid.uuid4().hex}"
    try:
        _safe_extract_tar_gz(archive, staging)
        manifest_path = staging / "better-agent-extension.json"
        if not manifest_path.is_file():
            raise ExtensionError(f"sync artifact for {extension_id} missing manifest")
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        if manifest["id"] != extension_id:
            raise ExtensionError(f"sync artifact id mismatch for {extension_id}")
        _reject_legacy_authority_replacement(
            current_records.get(extension_id),
            manifest,
        )
        _validate_declared_files(manifest, staging)
        _install_python_requirements(staging, manifest)
        smoke_test = _run_extension_smoke_test(manifest, staging)
        _write_package_completion(staging)
        with _package_publish_lock(target):
            _publish_package_dir(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    record["manifest"] = manifest
    record["smoke_test"] = smoke_test
    source["install_path"] = str(target)
    source.setdefault("commit_sha", str(artifact.get("commit_sha") or actual_sha))
    source["synced_artifact_sha256"] = actual_sha
    record["source"] = source


def _reconcile_after_sync(records: dict[str, Any]) -> dict[str, int]:
    instruction_swept = reconcile_all_instructions()
    skill_changes = reconcile_runtime_skills()
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
    current_records = copy.deepcopy(_load().get("extensions") or {})
    for record in records.values():
        source = record.get("source")
        if isinstance(source, dict):
            source.pop("install_path", None)
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise ExtensionError("extension sync artifacts must be a list")
    imported_artifacts: list[str] = []
    local_failures: list[dict[str, str]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            local_failures.append({
                "extension_id": f"artifact[{index}]",
                "error": "extension sync artifacts must be objects",
            })
            continue
        try:
            extension_id = _safe_sync_artifact_name(str(artifact.get("extension_id") or ""))
            _install_synced_artifact(artifact, records, current_records)
        except Exception as exc:
            raw_extension_id = str(artifact.get("extension_id") or "")
            try:
                failure_id = _safe_sync_artifact_name(raw_extension_id)
            except ExtensionError:
                failure_id = f"artifact[{index}]"
            local_failures.append({"extension_id": failure_id, "error": str(exc)})
            logger.warning("extension sync artifact %r failed: %s", failure_id, exc)
            current_record = current_records.get(failure_id)
            if isinstance(current_record, dict):
                records[failure_id] = copy.deepcopy(current_record)
            continue
        imported_artifacts.append(extension_id)
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
    # A package the primary could not build is missing on this node: its record
    # exists but no code arrived. Surface it in the result instead of reporting a
    # partial sync as a complete one.
    failures = [
        {
            "extension_id": str(item.get("extension_id") or ""),
            "error": str(item.get("error") or ""),
        }
        for item in (payload.get("artifact_failures") or [])
        if isinstance(item, dict)
    ] + local_failures
    if failures:
        logger.warning(
            "extension sync arrived without packages for: %s",
            ", ".join(sorted(item["extension_id"] for item in failures)),
        )
    return {
        "ok": not failures,
        "extension_count": len(records),
        "artifact_count": len(imported_artifacts),
        "imported_artifacts": imported_artifacts,
        "artifact_failures": failures,
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
    for daemon in entrypoints.get("daemons") or []:
        modules.add(daemon["module"])
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


def _validate_instructions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.instructions must be a list")
    items: list[dict[str, Any]] = []
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
        normalized: dict[str, Any] = {"name": name, "path": path, "level": level}
        providers_raw = item.get("providers")
        if providers_raw is not None:
            if not isinstance(providers_raw, list) or not providers_raw:
                raise ExtensionError(
                    "entrypoints.instructions.providers must be a non-empty list when present"
                )
            providers = [str(p).strip() for p in providers_raw]
            unknown = sorted(set(providers) - set(provider_kinds.all_provider_kinds()))
            if unknown:
                raise ExtensionError(
                    f"entrypoints.instructions.providers has unknown provider kinds: {', '.join(unknown)} "
                    f"(known: {sorted(provider_kinds.all_provider_kinds())})"
                )
            normalized["providers"] = sorted(set(providers))
        items.append(normalized)
    return items


def _validate_entrypoint_description(value: Any, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ExtensionError(f"{field} must be a string")
    description = value.strip()
    if len(description) > 500:
        raise ExtensionError(f"{field} must be at most 500 characters")
    return description


def _validate_skills(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.skills must be a list")
    items: list[dict[str, Any]] = []
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
        description = _validate_entrypoint_description(
            item.get("description"), field="entrypoints.skills.description"
        )
        cleaned: dict[str, Any] = {"name": name, "path": path, "description": description}
        if "default_enabled" in item:
            if not isinstance(item["default_enabled"], bool):
                raise ExtensionError("entrypoints.skills.default_enabled must be a boolean")
            cleaned["default_enabled"] = item["default_enabled"]
        if "requires_mcp" in item:
            requires = item["requires_mcp"]
            if isinstance(requires, bool):
                cleaned["requires_mcp"] = requires
            elif isinstance(requires, list):
                cleaned["requires_mcp"] = _validate_string_list(
                    requires, field="entrypoints.skills.requires_mcp"
                )
            else:
                raise ExtensionError(
                    "entrypoints.skills.requires_mcp must be a boolean or a list of MCP server names"
                )
        items.append(cleaned)
    return items


def _validate_agents(value: Any) -> list[dict[str, Any]]:
    """Subagent definitions materialized into each provider's native agent
    surface. One entry per agent; `providers` maps a provider id to that
    provider's native-format source file (e.g. claude .md, codex .toml).
    Providers not native to subagents are simply absent and get a no-op."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.agents must be a list")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.agents items must be objects")
        name = str(item.get("name") or "").strip()
        if not _ID_RE.fullmatch(name):
            raise ExtensionError("entrypoints.agents.name contains invalid characters")
        if name in seen:
            raise ExtensionError(f"entrypoints.agents contains duplicate name: {name}")
        seen.add(name)
        providers_raw = item.get("providers")
        if not isinstance(providers_raw, dict) or not providers_raw:
            raise ExtensionError(
                "entrypoints.agents.providers must be a non-empty object mapping provider id to source path"
            )
        providers: dict[str, str] = {}
        for provider_id, rel_path in providers_raw.items():
            provider_id = str(provider_id or "").strip()
            if provider_id not in _AGENT_TARGET_PROVIDERS:
                raise ExtensionError(
                    f"entrypoints.agents.providers has unsupported provider: {provider_id}"
                )
            providers[provider_id] = _clean_rel_path(
                str(rel_path or ""), field="entrypoints.agents.providers.path"
            )
        items.append({"name": name, "providers": providers})
    return items


def _validate_harness_profiles(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.harness_profiles must be a list")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.harness_profiles items must be objects")
        profile_id = str(item.get("id") or "").strip()
        if not _ID_RE.fullmatch(profile_id):
            raise ExtensionError("entrypoints.harness_profiles.id contains invalid characters")
        if profile_id == "default":
            raise ExtensionError("entrypoints.harness_profiles cannot declare default")
        if profile_id in seen:
            raise ExtensionError(f"entrypoints.harness_profiles contains duplicate id: {profile_id}")
        seen.add(profile_id)
        path = _clean_rel_path(str(item.get("path") or ""), field="entrypoints.harness_profiles.path")
        cleaned = {"id": profile_id, "path": path}
        if "name" in item:
            cleaned["name"] = str(item.get("name") or "").strip()
        if "description" in item:
            cleaned["description"] = _validate_entrypoint_description(
                item.get("description"), field="entrypoints.harness_profiles.description"
            )
        items.append(cleaned)
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


QUICK_BUTTON_PLACEMENTS = ("session", "settings")


def _validate_quick_button_placements(value: Any) -> list[str]:
    if value is None:
        return list(QUICK_BUTTON_PLACEMENTS)
    if not isinstance(value, list) or not value:
        raise ExtensionError(
            "entrypoints.quick_button.placements must be a non-empty array"
        )
    placements: list[str] = []
    for item in value:
        placement = str(item or "").strip()
        if placement not in QUICK_BUTTON_PLACEMENTS:
            allowed = ", ".join(QUICK_BUTTON_PLACEMENTS)
            raise ExtensionError(
                f"entrypoints.quick_button.placements entries must be one of: {allowed}"
            )
        if placement not in placements:
            placements.append(placement)
    return placements


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
        "placements": _validate_quick_button_placements(value.get("placements")),
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
            unknown = set(marker_raw) - {"color", "tooltip", "sound", "sound_setting"}
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
            sound_setting = marker_raw.get("sound_setting")
            if sound_setting is not None:
                # Names one of the extension's own boolean settings; the
                # sound only plays while that setting is on. Cross-checked
                # against entrypoints.settings in validate_manifest.
                if not isinstance(sound_setting, str) or not _SETTING_KEY_RE.fullmatch(sound_setting):
                    raise ExtensionError(
                        f"{prefix}.marker.sound_setting must be a lowercase snake_case setting key"
                    )
                marker["sound_setting"] = sound_setting
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


def _validate_settings_sections(value: Any) -> list[dict[str, Any]]:
    """Sections an extension adds to the app Settings page. A setting that
    names one here is app-wide (one global value) instead of a per-profile
    harness overlay."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.settings_sections must be a list")
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ExtensionError(f"entrypoints.settings_sections[{index}] must be an object")
        section_id = str(raw.get("id") or "").strip()
        if not _SETTING_KEY_RE.fullmatch(section_id):
            raise ExtensionError(
                f"entrypoints.settings_sections[{index}].id must be a lowercase snake_case identifier"
            )
        if section_id in seen:
            raise ExtensionError(
                f"entrypoints.settings_sections contains duplicate id: {section_id}"
            )
        seen.add(section_id)
        label = str(raw.get("label") or "").strip()
        if not label:
            raise ExtensionError(f"entrypoints.settings_sections[{index}].label is required")
        section: dict[str, Any] = {"id": section_id, "label": label}
        description = str(raw.get("description") or "").strip()
        if description:
            section["description"] = description
        sections.append(section)
    return sections


def _validate_settings(
    value: Any, *, sections: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Declarative config fields an extension surfaces in Settings.

    Stored values are user-supplied; ``secret`` types route to the OS
    keychain (never plaintext). List order is the author's display order.
    A ``section`` naming a declared ``settings_sections`` entry moves the
    field to the app Settings page as one app-wide value.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.settings must be a list")
    known_sections = {section["id"] for section in sections or []}
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
        section_id = str(raw.get("section") or "").strip()
        if section_id:
            if section_id not in known_sections:
                raise ExtensionError(
                    f"entrypoints.settings[{index}].section is not declared in "
                    f"entrypoints.settings_sections: {section_id}"
                )
            if setting_type == "secret":
                raise ExtensionError(
                    f"entrypoints.settings[{index}] of type secret cannot declare a section"
                )
            item["section"] = section_id
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


def _mcp_user_facing_value(item: dict[str, Any]) -> bool:
    if "user_facing" in item:
        value = item.get("user_facing")
        if not isinstance(value, bool):
            raise ExtensionError("entrypoints.mcp.user_facing must be a boolean")
        return value
    if "interacts_with_user" in item:
        value = item.get("interacts_with_user")
        if not isinstance(value, bool):
            raise ExtensionError("entrypoints.mcp.interacts_with_user must be a boolean")
        return value
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
        if (
            replaces_builtin not in _BUILTIN_MCP_REPLACEMENTS_BY_EXTENSION_ID.get(extension_id, frozenset())
            and replaces_builtin not in _MCP_REPLACEMENT_CORE_ROLES
        ):
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
        ambient_native = item.get("ambient_native", False)
        if not isinstance(ambient_native, bool):
            raise ExtensionError("entrypoints.mcp.ambient_native must be a boolean")
        user_facing = _mcp_user_facing_value(item)
        requires_backend_auth = item.get("requires_backend_auth") is not False
        predicate = _validate_mcp_predicate(item.get("predicate"))
        if ambient_native and (user_facing or requires_backend_auth or predicate):
            raise ExtensionError(
                "entrypoints.mcp.ambient_native requires user_facing=false, "
                "requires_backend_auth=false, and no predicate"
            )
        label = str(item.get("label") or "").strip()
        if label and len(label) > 80:
            raise ExtensionError("entrypoints.mcp.label must be at most 80 characters")
        description = _validate_entrypoint_description(item.get("description"), field="entrypoints.mcp.description")
        default_enabled = item.get("default_enabled", True)
        if not isinstance(default_enabled, bool):
            raise ExtensionError("entrypoints.mcp.default_enabled must be a boolean")
        items.append(
            {
                "name": name,
                "label": label,
                "description": description,
                "default_enabled": default_enabled,
                "python": python_path,
                "module": module,
                "command": command,
                "args": args,
                "env": env,
                "user_facing": user_facing,
                "bare_allowed": item.get("bare_allowed") is True,
                "requires_backend_auth": requires_backend_auth,
                "ambient_native": ambient_native,
                "replaces_builtin": replaces_builtin,
                "predicate": predicate,
            }
        )
    return items


def _requirements_role_mcp_item(record: dict[str, Any], item: dict[str, Any]) -> bool:
    manifest = record.get("manifest") or {}
    if manifest.get("id") != extension_id_for_role("requirements"):
        return False
    return (
        str(item.get("name") or "") == "better-agent-requirements"
        or str(item.get("replaces_builtin") or "") == "get-requirements"
    )


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
        if _requirements_role_mcp_item(record, raw):
            raw = {**raw, "user_facing": False}
        items.append(raw)
    return items


# Session-record fields an extension may mutate via the scoped
# /api/internal/session-field endpoint. Each maps to a tested session_manager
# setter; the extension declares a subset under permissions.mutates_session_fields.
_MUTABLE_SESSION_FIELDS = frozenset({
    "supervisor_enabled",
    "pending_supervisor_verdict",
    "clear_pending_supervisor_verdict",
    "current_todos",
    "current_tasks",
})
_READABLE_SESSION_FIELDS = frozenset({
    "current_todos",
    "current_tasks",
})


def _validate_native_mcp_permission(value: Any) -> dict[str, list[str]]:
    """permissions.native_mcp declares, per native MCP server this extension
    exposes (matched against entrypoints.mcp[].name), which scopes it's
    ELIGIBLE for -- e.g. {"cards": ["global", "project"]}. This is the one
    install-time review point (native_mcp_grants.py's docstring calls this
    "the declaration"); actually GRANTING a scope (creating the narrower
    record that makes a server resolve) is a separate step -- see
    grant_native_mcp_server() -- and can never grant a scope not listed
    here."""
    if not isinstance(value, dict):
        raise ExtensionError("permissions.native_mcp must be an object of server_id -> scope list")
    validated: dict[str, list[str]] = {}
    for server_id, scopes in value.items():
        if not isinstance(server_id, str) or not _ID_RE.fullmatch(server_id):
            raise ExtensionError(f"permissions.native_mcp has an invalid server id: {server_id!r}")
        if not isinstance(scopes, list) or not scopes:
            raise ExtensionError(f"permissions.native_mcp.{server_id} must be a non-empty list of scopes")
        bad_scopes = sorted(set(scopes) - set(native_mcp_grants.VALID_SCOPES))
        if bad_scopes:
            raise ExtensionError(
                f"permissions.native_mcp.{server_id} has invalid scopes: {', '.join(bad_scopes)}"
            )
        validated[server_id] = sorted(set(scopes))
    return validated


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
        "marketplace_auth",
        "reads_session_fields",
        "mutates_session_fields",
        "managed_run_env",
        "capabilities",
        "in_process_execution",
        "daemons",
        "native_mcp",
        "internal_llm_tasks",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExtensionError(f"permissions contains unknown keys: {', '.join(unknown)}")
    permissions: dict[str, Any] = {}
    for key, item in value.items():
        if key == "capabilities":
            if not isinstance(item, list) or not all(
                isinstance(part, str) and part.strip() for part in item
            ):
                raise ExtensionError("permissions.capabilities must be a string list")
            permissions[key] = [part.strip() for part in item]
            continue
        if key == "native_mcp":
            permissions[key] = _validate_native_mcp_permission(item)
            continue
        if key == "daemons":
            if item not in ("backend", "supervisor"):
                raise ExtensionError("permissions.daemons must be 'backend' or 'supervisor'")
            permissions[key] = item
            continue
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
    declared_capabilities = permissions.get("capabilities")
    if declared_capabilities is not None:
        bad = sorted(
            item for item in declared_capabilities
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}\.[a-z0-9][a-z0-9._-]{0,127}", item)
        )
        if bad:
            raise ExtensionError(
                f"permissions.capabilities has invalid grants: {', '.join(bad)}"
            )
    declared_tasks = permissions.get("internal_llm_tasks")
    if declared_tasks is not None:
        bad = sorted(
            item for item in declared_tasks
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
        )
        if bad:
            raise ExtensionError(
                f"permissions.internal_llm_tasks has invalid task keys: {', '.join(bad)}"
            )
    return permissions


def _validate_daemons(value: Any, *, extension_id: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExtensionError("entrypoints.daemons must be a list")
    daemons: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ExtensionError("entrypoints.daemons entries must be objects")
        unknown = sorted(set(item) - {
            "name", "module", "lifecycle", "retire_policy",
            "restart_policy", "env_allowlist", "ports",
        })
        if unknown:
            raise ExtensionError(f"entrypoints.daemons entry has unknown keys: {', '.join(unknown)}")
        name = str(item.get("name") or "").strip()
        if not _DAEMON_NAME_RE.fullmatch(name):
            raise ExtensionError("entrypoints.daemons name must be 1-40 lowercase letters, digits, or hyphens")
        if name in seen_names:
            raise ExtensionError(f"entrypoints.daemons has duplicate name: {name!r}")
        seen_names.add(name)
        module = _clean_optional_python_module(item.get("module"), field="entrypoints.daemons.module")
        if not module:
            raise ExtensionError("entrypoints.daemons entries require a module")
        lifecycle = str(item.get("lifecycle") or "").strip()
        if lifecycle not in _DAEMON_LIFECYCLES:
            raise ExtensionError(
                "entrypoints.daemons lifecycle must be one of: " + ", ".join(sorted(_DAEMON_LIFECYCLES))
            )
        retire_policy = str(item.get("retire_policy") or "immediate").strip()
        if retire_policy not in {"immediate", "drain"}:
            raise ExtensionError(
                "entrypoints.daemons retire_policy must be one of: drain, immediate"
            )
        if retire_policy == "drain" and lifecycle != "supervisor":
            raise ExtensionError(
                "entrypoints.daemons retire_policy='drain' requires supervisor lifecycle"
            )
        restart_policy_raw = item.get("restart_policy") or {}
        if not isinstance(restart_policy_raw, dict):
            raise ExtensionError("entrypoints.daemons restart_policy must be an object")
        unknown_policy = sorted(set(restart_policy_raw) - {"max_restarts", "backoff_seconds"})
        if unknown_policy:
            raise ExtensionError(
                f"entrypoints.daemons restart_policy has unknown keys: {', '.join(unknown_policy)}"
            )
        max_restarts = restart_policy_raw.get("max_restarts", 5)
        if not isinstance(max_restarts, int) or isinstance(max_restarts, bool) or not (0 <= max_restarts <= 100):
            raise ExtensionError("entrypoints.daemons restart_policy.max_restarts must be an int in 0..100")
        backoff_seconds = restart_policy_raw.get("backoff_seconds", 5)
        if not isinstance(backoff_seconds, (int, float)) or isinstance(backoff_seconds, bool) or not (
            1 <= backoff_seconds <= 3600
        ):
            raise ExtensionError("entrypoints.daemons restart_policy.backoff_seconds must be in 1..3600")
        env_allowlist = _validate_string_list(item.get("env_allowlist"), field="entrypoints.daemons.env_allowlist")
        bad_env = sorted(key for key in env_allowlist if not _DAEMON_ENV_KEY_RE.fullmatch(key))
        if bad_env:
            raise ExtensionError(f"entrypoints.daemons env_allowlist has invalid env keys: {', '.join(bad_env)}")
        ports_raw = item.get("ports")
        ports: list[int] = []
        if ports_raw is not None:
            if not isinstance(ports_raw, list):
                raise ExtensionError("entrypoints.daemons ports must be a list of ints")
            for port in ports_raw:
                if not isinstance(port, int) or isinstance(port, bool) or not (1024 <= port <= 65535):
                    raise ExtensionError("entrypoints.daemons ports entries must be ints in 1024..65535")
                if port in _DAEMON_RESERVED_PORTS:
                    raise ExtensionError(f"entrypoints.daemons port {port} is reserved by the platform")
                ports.append(port)
        daemons.append(
            {
                "name": name,
                "module": module,
                "lifecycle": lifecycle,
                "retire_policy": retire_policy,
                "restart_policy": {"max_restarts": max_restarts, "backoff_seconds": backoff_seconds},
                "env_allowlist": env_allowlist,
                "ports": ports,
            }
        )
    return daemons


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


_ALLOWED_HOOK_KEYS = (
    "pre_turn",
    "post_turn",
    "session_event",
    "pre_send_advisory",
    "provider_transport",
)


def _validate_hooks(value: Any, *, has_backend: bool) -> dict[str, Any]:
    """Declarative lifecycle hooks an extension subscribes to. Today:
    ``pre_turn`` — core invokes fire-and-forget before a turn runs (on
    ``lifecycle.turn_start``); ``post_turn`` — core invokes fire-and-forget
    after ``lifecycle.turn_complete``; ``session_event`` — per session event;
    ``pre_send_advisory`` — core queries synchronously before a prompt is
    sent and surfaces returned advisories to the user; ``provider_transport``
    — core synchronously resolves the versioned, validated local transport
    descriptor before composing a provider subprocess environment. Every hook
    is a backend invocation and requires ``entrypoints.backend``."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ExtensionError("entrypoints.hooks must be an object")
    hooks: dict[str, Any] = {}
    for key in _ALLOWED_HOOK_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        if not has_backend:
            raise ExtensionError(f"entrypoints.hooks.{key} requires entrypoints.backend")
        path = str(raw).strip()
        if not path.startswith("/"):
            raise ExtensionError(f"entrypoints.hooks.{key} must be a path starting with /")
        hooks[key] = path
    unknown = sorted(set(value) - set(_ALLOWED_HOOK_KEYS))
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


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def resolve_route_timeout(
    backend_timeouts: Any, path: str, *, default_seconds: float
) -> float:
    """Canonical "how long is this backend route allowed to take" resolution.

    Single source of truth for the manifest-declared per-route budget: the host
    uses it to time out a roundtrip and the quarantine path uses it to decide
    what counts as a slow call, so a route cannot be granted 360s by one
    subsystem and punished at the global floor by another.

    Lookup order: exact route subpath, then longest segment-prefix, then the
    special ``default`` key, then ``default_seconds``.
    """
    if not isinstance(backend_timeouts, dict) or not backend_timeouts:
        return float(default_seconds)
    p = str(path or "").strip("/")
    chosen = backend_timeouts.get(p)
    if not _is_positive_number(chosen):
        best_len = -1
        for key, value in backend_timeouts.items():
            if key == "default" or not _is_positive_number(value):
                continue
            k = str(key).strip("/")
            if (p == k or p.startswith(k + "/")) and len(k) > best_len:
                best_len, chosen = len(k), value
        if best_len < 0:
            chosen = backend_timeouts.get("default")
    if _is_positive_number(chosen):
        return float(chosen)
    return float(default_seconds)


def slow_call_threshold(backend_timeouts: Any, path: str) -> float:
    """Seconds one call to backend route ``path`` may take before it counts as
    slow: the route's declared budget, or ``EXTENSION_SLOW_CALL_SECONDS`` when it
    declares none. Callers on the request path use it to skip the store write for
    a call that cannot be an incident; the quarantine path uses it to judge one.
    """
    return resolve_route_timeout(
        backend_timeouts, path, default_seconds=EXTENSION_SLOW_CALL_SECONDS
    )


def _record_slow_call_threshold(record: dict[str, Any], route_path: str) -> float:
    return slow_call_threshold(
        ((record.get("manifest") or {}).get("entrypoints") or {}).get("backend_timeouts"),
        route_path,
    )


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
    _settings_sections = _validate_settings_sections(entrypoints_raw.get("settings_sections"))
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
        "agents": _validate_agents(entrypoints_raw.get("agents")),
        "harness_profiles": _validate_harness_profiles(entrypoints_raw.get("harness_profiles")),
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
        "settings_sections": _settings_sections,
        "settings": _validate_settings(
            entrypoints_raw.get("settings"), sections=_settings_sections
        ),
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
        "daemons": _validate_daemons(entrypoints_raw.get("daemons"), extension_id=extension_id),
    }
    if entrypoints["frontend"] and len(Path(entrypoints["frontend"]).parts) < 2:
        raise ExtensionError("entrypoints.frontend must live under a dedicated asset directory")
    permissions = _validate_permissions(raw.get("permissions"))
    if entrypoints["remote_services"] and permissions.get("network") is not True:
        raise ExtensionError("entrypoints.remote_services requires permissions.network=true")
    if entrypoints["daemons"]:
        if "daemons" not in surfaces:
            raise ExtensionError("entrypoints.daemons requires the 'daemons' surface")
        declared_level = permissions.get("daemons")
        needs_supervisor = any(d["lifecycle"] == "supervisor" for d in entrypoints["daemons"])
        if needs_supervisor and declared_level != "supervisor":
            raise ExtensionError(
                "supervisor-lifecycle daemons require permissions.daemons='supervisor'"
            )
        if declared_level not in ("backend", "supervisor"):
            raise ExtensionError(
                "entrypoints.daemons requires permissions.daemons='backend' or 'supervisor'"
            )
    _boolean_setting_keys = {
        item["key"] for item in entrypoints["settings"] if item["type"] == "boolean"
    }
    for rule in entrypoints["applied_config"].get("tag_rules") or []:
        gate = (rule.get("marker") or {}).get("sound_setting")
        if gate and gate not in _boolean_setting_keys:
            raise ExtensionError(
                f"tag_rules[{rule['tag']}].marker.sound_setting must name a declared "
                f"boolean entrypoints.settings key: {gate}"
            )
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
        "core_roles": _validate_string_list(raw.get("core_roles"), field="core_roles"),
        "protocol": (
            _validate_protocol(raw.get("protocol"))
            if "protocol" in raw
            else _default_protocol_for_entrypoints(entrypoints)
        ),
        "marketplace": marketplace,
    }
    unknown_roles = sorted(set(manifest["core_roles"]) - CORE_ROLES)
    if unknown_roles:
        raise ExtensionError(f"core_roles contains unknown values: {', '.join(unknown_roles)}")
    for item in manifest["entrypoints"]["mcp"]:
        replacement = item.get("replaces_builtin")
        required_role = _MCP_REPLACEMENT_CORE_ROLES.get(replacement)
        if required_role and required_role not in manifest["core_roles"]:
            raise ExtensionError(
                f"entrypoints.mcp.replaces_builtin={replacement!r} requires core_roles={required_role!r}"
            )
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


def _marketplace_base_url() -> str:
    raw = str(os.environ.get("BETTER_AGENT_MARKETPLACE_BASE_URL") or _DEFAULT_MARKETPLACE_BASE_URL).strip()
    return raw.rstrip("/")


def _marketplace_public_key() -> str:
    return str(os.environ.get("BETTER_AGENT_MARKETPLACE_PUBLIC_KEY") or _DEFAULT_MARKETPLACE_PUBLIC_KEY).strip()


def verify_marketplace_catalog_snapshot(
    *,
    catalog: dict[str, Any],
    signature: str,
    snapshot_id: str,
    extension_id: str,
    expected_version: str,
    publisher_fingerprint: str,
    permission_hash: str,
    minimum_sequence: int,
    allow_expired: bool = False,
) -> dict[str, Any]:
    if not isinstance(catalog, dict) or set(catalog) != {
        "key_id",
        "sequence",
        "issued_at",
        "expires_at",
        "extensions",
    }:
        raise ExtensionError("marketplace catalog has an invalid shape")
    key_id = str(catalog["key_id"])
    if key_id != "default-v1":
        raise ExtensionError("marketplace catalog signing key is unknown")
    sequence = catalog["sequence"]
    if not isinstance(sequence, int) or sequence < minimum_sequence:
        raise ExtensionError("marketplace catalog sequence is stale")
    try:
        issued_at = datetime.fromisoformat(str(catalog["issued_at"]).replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(catalog["expires_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExtensionError("marketplace catalog timestamps are invalid") from exc
    now = datetime.now(timezone.utc)
    if (
        issued_at.tzinfo is None
        or expires_at.tzinfo is None
        or issued_at > now
        or (expires_at <= now and not allow_expired)
    ):
        raise ExtensionError("marketplace catalog is expired or not yet valid")
    extensions = catalog["extensions"]
    if not isinstance(extensions, dict):
        raise ExtensionError("marketplace catalog extensions are invalid")
    canonical = json.dumps(
        catalog,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    catalog_hash = hashlib.sha256(canonical).hexdigest()
    if snapshot_id != f"{key_id}:{sequence}:{catalog_hash}":
        raise ExtensionError("marketplace catalog snapshot does not match action")
    key_bytes = _decode_key_or_signature(_marketplace_public_key(), "marketplace public key")
    signature_bytes = _decode_key_or_signature(signature, "catalog signature")
    if len(key_bytes) != 32:
        raise ExtensionError("marketplace public key must be an Ed25519 public key")
    try:
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature_bytes, canonical)
    except InvalidSignature as exc:
        raise ExtensionError("marketplace catalog signature is invalid") from exc
    metadata = extensions.get(extension_id)
    if not isinstance(metadata, dict):
        raise ExtensionError("marketplace extension is absent from approved catalog")
    metadata_id = str(metadata.get("extension_id") or metadata.get("id") or "")
    if metadata_id != extension_id or str(metadata.get("version") or "") != expected_version:
        raise ExtensionError("marketplace catalog target does not match action")
    if str(metadata.get("publisher_fingerprint") or "") != publisher_fingerprint:
        raise ExtensionError("marketplace publisher fingerprint does not match action")
    if str(metadata.get("permission_hash") or "") != permission_hash:
        raise ExtensionError("marketplace permission hash does not match action")
    return {
        "metadata": copy.deepcopy(metadata),
        "key_id": key_id,
        "sequence": sequence,
    }


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
    # Read the archive from memory. Any on-disk scratch copy named after the
    # install root is shared by concurrent installs of sibling versions, which
    # lets one writer truncate an archive another is still reading.
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ExtensionError("marketplace artifact contains unsafe paths")
                resolved = (target / member.name).resolve()
                if not resolved.is_relative_to(target.resolve()):
                    raise ExtensionError("marketplace artifact path escapes package root")
                if member.islnk() or member.issym():
                    raise ExtensionError("marketplace artifact must not contain links")
            archive.extractall(target)
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise ExtensionError("marketplace artifact is not a valid tar.gz") from exc


# Runtime/build trees, never package content: they are platform-specific (a
# macOS venv is useless on a Windows node), they are rebuilt on the target from
# the package's own manifests, and they routinely contain symlinks that the
# package guard below would otherwise reject.
_PACKAGE_ARTIFACT_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
_PACKAGE_COMPLETION_MARKER = ".better-agent-package-complete.json"
_PACKAGE_COMPLETION_VERSION = 1


def _package_artifact_paths(package_dir: Path) -> list[Path]:
    """Files to ship for ``package_dir``, pruning runtime/build subtrees.

    Pruning happens before the symlink guard so a symlinked ``node_modules``
    is skipped rather than failing the package; the guard still rejects links
    anywhere in real package content.
    """
    found: list[Path] = []
    stack = [package_dir]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if entry.name in _PACKAGE_ARTIFACT_SKIP_DIRS:
                continue
            if entry.is_symlink():
                raise ExtensionError("extension package must not contain links")
            if entry.is_dir():
                stack.append(entry)
                continue
            if not entry.is_file():
                raise ExtensionError("extension package contains unsupported filesystem entries")
            found.append(entry)
    return sorted(found)


def _build_package_artifact(package_dir: Path) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gzip_file:
        with tarfile.open(fileobj=gzip_file, mode="w") as archive:
            for path in _package_artifact_paths(package_dir):
                rel = path.relative_to(package_dir).as_posix()
                # Snapshot the bytes and declare that exact size, so a package
                # file rewritten mid-build cannot emit a header promising more
                # bytes than follow and yield an unreadable archive.
                data = path.read_bytes()
                info = archive.gettarinfo(str(path), arcname=rel)
                info.size = len(data)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _package_content_hashes(root: Path) -> dict[str, str]:
    marker = root / _PACKAGE_COMPLETION_MARKER
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _package_artifact_paths(root)
        if path != marker
    }


def _write_package_completion(target: Path) -> None:
    files = _package_content_hashes(target)
    if "better-agent-extension.json" not in files:
        raise ExtensionError("extension package manifest is missing")
    (target / _PACKAGE_COMPLETION_MARKER).write_text(
        json.dumps(
            {"version": _PACKAGE_COMPLETION_VERSION, "files": files},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _package_published(target: Path) -> bool:
    marker = target / _PACKAGE_COMPLETION_MARKER
    try:
        if marker.stat().st_size > 4 * 1024 * 1024:
            return False
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        files = payload.get("files")
        return (
            payload.get("version") == _PACKAGE_COMPLETION_VERSION
            and isinstance(files, dict)
            and files == _package_content_hashes(target)
        )
    except (ExtensionError, FileNotFoundError, OSError, ValueError, TypeError):
        return False


def _package_publish_lock(target: Path) -> threading.Lock:
    key = str(target)
    with _PACKAGE_PUBLISH_LOCK_GUARD:
        return _PACKAGE_PUBLISH_LOCKS.setdefault(key, threading.Lock())


def _publish_package_dir(staging: Path, target: Path) -> bool:
    """Move a fully extracted staging tree onto ``target``.

    Returns True when this call published the tree. Another process may hold
    the same content-addressed path, so a lost race keeps the winner's tree and
    returns False rather than replacing equivalent content underneath readers.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if _package_published(target):
        return False
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
    except FileNotFoundError:
        pass
    if not target.exists():
        os.replace(staging, target)
        return True
    retired = target.parent / f".retired-{uuid.uuid4().hex}"
    try:
        os.replace(target, retired)
    except FileNotFoundError:
        os.replace(staging, target)
        return True
    try:
        os.replace(staging, target)
    except OSError:
        os.replace(retired, target)
        raise
    shutil.rmtree(retired, ignore_errors=True)
    return True


def _install_package_artifact(package_dir: Path, target: Path) -> bool:
    """Install ``package_dir`` at ``target`` without destroying a live tree.

    Live extension records point at ``target`` and readiness checks stat it, so
    the tree is built and extracted into a private staging directory and only
    then moved into place. A published tree is left untouched: the path is
    content-addressed, so rebuilding it in place would be redundant work on a
    path readers stat continuously. Returns True when this call published
    ``target``.
    """
    # Serialize per target so concurrent reconcile passes cannot each build and
    # then swap equivalent content underneath one another.
    with _package_publish_lock(target):
        if _package_published(target):
            return False
        staging = target.parent / f".staging-{uuid.uuid4().hex}"
        try:
            _safe_extract_tar_gz(_build_package_artifact(package_dir), staging)
            _write_package_completion(staging)
            return _publish_package_dir(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _install_from_package_dir(
    *,
    package_dir: Path,
    source: dict[str, str],
    entitlement_token: str = "",
    force_enabled: bool = False,
    default_enabled: bool = False,
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
    created = _install_package_artifact(package_dir, target)
    try:
        _install_python_requirements(target, manifest)
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
        # Only discard a tree this call created. The path is content-addressed,
        # so a pre-existing one is already referenced by a live record.
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise

    now = _now()
    record = {
        "manifest": manifest,
        # Fresh installs arrive inert: installing third-party code must not also
        # start running it. An update keeps whatever the user already chose.
        "enabled": True if force_enabled or manifest["id"] in REQUIRED_EXTENSION_IDS else existing.get("enabled", default_enabled),
        "activation_id": uuid.uuid4().hex,
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
    if existing.get("quarantine"):
        record["quarantine"] = copy.deepcopy(existing["quarantine"])
    if persist:
        previous_data = _load()
        data = copy.deepcopy(previous_data)
        data["extensions"][manifest["id"]] = record
        recovered = _recover_quarantined_cohort_for_generation(data, manifest["id"], existing, record)
        try:
            _save(data, resurrect_extension_ids={manifest["id"]})
            if previous_exists:
                _evict_extension_backend(manifest["id"])
            _reconcile_recovered_cohorts(data, recovered)
            if not recovered:
                extension_applied_config.reconcile(record)
                reconcile_runtime_skills()
        except Exception:
            _save(previous_data)
            for recovered_id in recovered:
                _evict_extension_backend(recovered_id)
            raise
        _drop_update_cache_row(manifest["id"])
    return record


def _evict_extension_backend(extension_id: str) -> None:
    from extension_backend_loader import evict_persistent_backend

    evict_persistent_backend(extension_id)


def _record_generation(record: dict[str, Any]) -> str:
    source = record.get("source") or {}
    return str(source.get("package_sha256") or source.get("commit_sha") or "")


def _recover_quarantined_cohort_for_generation(
    data: dict[str, Any],
    trigger_id: str,
    previous: dict[str, Any],
    refreshed: dict[str, Any],
) -> list[str]:
    quarantine = previous.get("quarantine") or {}
    if quarantine.get("attributed_extension_id") != trigger_id:
        return []
    previous_generation = str(quarantine.get("attributed_generation") or "")
    if not previous_generation or previous_generation != _record_generation(previous):
        return []
    if _record_generation(refreshed) == previous_generation:
        return []
    cohort = quarantine.get("cohort")
    if not isinstance(cohort, list) or not cohort or trigger_id not in cohort:
        return []
    cohort_ids = [str(item) for item in cohort]
    if len(set(cohort_ids)) != len(cohort_ids):
        return []
    records = data.get("extensions") or {}
    candidates: dict[str, dict[str, Any]] = {}
    for extension_id in cohort_ids:
        candidate = refreshed if extension_id == trigger_id else records.get(extension_id)
        if not isinstance(candidate, dict) or candidate.get("enabled") is not False:
            return []
        candidate_quarantine = candidate.get("quarantine") or {}
        if (
            candidate_quarantine.get("attributed_extension_id") != trigger_id
            or candidate_quarantine.get("attributed_generation") != previous_generation
            or candidate_quarantine.get("cohort") != cohort
        ):
            return []
        stored_manifest = json.loads(json.dumps(candidate.get("manifest") or {}))
        stored_entrypoints = stored_manifest.get("entrypoints") or {}
        for key in list(stored_entrypoints):
            if stored_entrypoints[key] is None:
                stored_entrypoints.pop(key)
        for optional_surface in ("quick_button", "page"):
            surface = stored_entrypoints.get(optional_surface)
            if isinstance(surface, dict) and not surface.get("label"):
                stored_entrypoints.pop(optional_surface, None)
        manifest = validate_manifest(stored_manifest)
        if manifest["id"] != extension_id:
            return []
        if not _entitlement_active(candidate.get("entitlement") or {}):
            return []
        if consent_required(candidate):
            return []
        if not _record_backend_surface_ready(candidate):
            return []
        candidates[extension_id] = candidate

    ordered: list[str] = []
    pending = set(cohort_ids)
    while pending:
        progressed = False
        for extension_id in sorted(pending):
            dependencies = set((candidates[extension_id].get("manifest") or {}).get("dependencies") or [])
            if dependencies.intersection(pending):
                continue
            for dependency in dependencies:
                dep = candidates.get(dependency) or records.get(dependency)
                if not isinstance(dep, dict) or (dependency not in candidates and dep.get("enabled") is not True):
                    return []
            ordered.append(extension_id)
            pending.remove(extension_id)
            progressed = True
        if not progressed:
            return []

    for extension_id in ordered:
        candidate = candidates[extension_id]
        candidate["enabled"] = True
        _rotate_activation_identity(candidate)
        candidate.pop("quarantine", None)
        candidate["updated_at"] = _now()
        records[extension_id] = candidate
    return ordered


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_bin_dir(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def _extension_python(install_root: Path, *, has_dependency_environment: bool) -> Path:
    extension_python = _venv_python(install_root / ".venv")
    if has_dependency_environment:
        if not extension_python.is_file():
            raise ExtensionError("extension dependency environment is missing")
        try:
            result = subprocess.run(
                [str(extension_python), "-c", "import sys; sys.exit(0)"],
                cwd=install_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExtensionError(
                "extension dependency environment is not runnable"
            ) from exc
        if result.returncode != 0:
            raise ExtensionError("extension dependency environment is not runnable")
        return extension_python
    try:
        return dependency_plan.verified_active_python(Path(__file__).resolve().parent)
    except dependency_plan.DependencyPlanError as exc:
        raise ExtensionError(
            "extension runtime requires an active backend dependency environment"
        ) from exc


def _sdk_runtime_requirements_path() -> Path:
    frozen_root = str(getattr(sys, "_MEIPASS", "") or "")
    if frozen_root:
        return Path(frozen_root) / "sdk" / "runtime-requirements.txt"
    return (
        Path(__file__).resolve().parent.parent
        / "sdk"
        / "runtime-requirements.txt"
    )


def _install_python_requirements(target: Path, manifest: dict[str, Any]) -> None:
    requirements = list(manifest.get("entrypoints", {}).get("python_requirements") or [])
    if not requirements:
        return
    if os.environ.get("BETTER_AGENT_SKIP_EXTENSION_DEPENDENCY_INSTALL") == "1":
        return
    sdk_requirements = _sdk_runtime_requirements_path()
    if not sdk_requirements.is_file():
        raise ExtensionError("extension SDK runtime requirements are unavailable")
    venv_dir = target / ".venv"
    try:
        backend_python = dependency_plan.verified_active_python(
            Path(__file__).resolve().parent
        )
    except dependency_plan.DependencyPlanError as exc:
        raise ExtensionError(
            "extension dependency environment creation requires an active "
            "backend dependency environment"
        ) from exc
    result = subprocess.run(
        [str(backend_python), "-m", "venv", str(venv_dir)],
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
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-r",
            str(sdk_requirements),
            *requirements,
        ],
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
    name = _EXTENSION_DISPLAY_NAMES.get(extension_id, extension_id)
    extension_path = _PUBLIC_EXTENSION_PATHS.get(extension_id, "")
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
                "agents": [],
            },
            "permissions": {},
            "marketplace": {
                "product_id": "",
                "subscription_required": False,
                "entitlement_url": "",
            },
        },
        "enabled": required,
        "activation_id": uuid.uuid4().hex,
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
    managed_ids = set(_PUBLIC_EXTENSION_PATHS)
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
                "activation_id": uuid.uuid4().hex,
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
    extension_path = _PUBLIC_EXTENSION_PATHS.get(extension_id)
    if not extension_path:
        return False
    roots = [_repo_root()]
    configured = _required_marketplace_repo_root()
    if configured is not None:
        roots.insert(0, configured)
    return any((root / extension_path).exists() for root in roots)



def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _hash_public_package(package_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*")):
        if (
            not path.is_file()
            or any(part in {"__pycache__", ".pytest_cache", ".venv"} for part in path.parts)
            or path.suffix == ".pyc"
        ):
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
    created = _install_package_artifact(package_dir, target)
    try:
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    now = _now()
    return {
        "manifest": manifest,
        "enabled": True,
        "activation_id": uuid.uuid4().hex,
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


def _local_package_from_record(record: dict[str, Any]) -> Path | None:
    source = record.get("source") or {}
    if source.get("type") != "better_agent_local":
        return None
    root_text = str(source.get("repo_url") or "").strip()
    if not root_text or "://" in root_text:
        return None
    try:
        root = Path(root_text).expanduser().resolve()
        allowed_roots = {_repo_root().resolve()}
        configured_root = _required_marketplace_repo_root()
        if configured_root is not None:
            allowed_roots.add(configured_root.resolve())
        if root not in allowed_roots:
            return None
        relative = _clean_rel_path(
            str(source.get("extension_path") or ""),
            field="source.extension_path",
        )
        package = (root / relative).resolve()
        if not package.is_relative_to(root):
            return None
        if not (package / "better-agent-extension.json").is_file():
            return None
        return package
    except (ExtensionError, OSError):
        return None


def _refresh_local_extension_snapshot(
    extension_id: str,
    record: dict[str, Any],
    package_dir: Path,
    package_sha: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    _validate_declared_files(manifest, package_dir)
    target = _install_root() / extension_id / "versions" / package_sha
    created = _install_package_artifact(package_dir, target)
    try:
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    refreshed = copy.deepcopy(record)
    refreshed["manifest"] = manifest
    refreshed["updated_at"] = _now()
    refreshed["smoke_test"] = smoke_test
    refreshed["source"] = {
        **(record.get("source") or {}),
        "package_sha256": package_sha,
        "install_path": str(target),
    }
    _rotate_activation_identity(refreshed)
    return refreshed


def _reconcile_recovered_cohorts(data: dict[str, Any], recovered: list[str]) -> None:
    import extension_token_registry

    for extension_id in recovered:
        record = data["extensions"][extension_id]
        if needs_identity_token(record):
            extension_token_registry.mint(extension_id)
        extension_applied_config.reconcile(record)
    if recovered:
        reconcile_runtime_skills()


def _ensure_local_extensions(data: dict[str, Any]) -> tuple[bool, list[str]]:
    changed = False
    recovered: list[str] = []
    for extension_id, record in list((data.get("extensions") or {}).items()):
        if not isinstance(record, dict):
            continue
        package_dir = _local_package_from_record(record)
        if package_dir is None:
            continue
        try:
            manifest = validate_manifest(json.loads(
                (package_dir / "better-agent-extension.json").read_text(encoding="utf-8")
            ))
            if manifest["id"] != extension_id:
                continue
            package_sha = _hash_public_package(package_dir)
            source = record.get("source") or {}
            install_path = Path(str(source.get("install_path") or ""))
            if (
                source.get("package_sha256") == package_sha
                and manifest == record.get("manifest")
                and install_path.is_dir()
            ):
                continue
            refreshed = _refresh_local_extension_snapshot(
                extension_id,
                record,
                package_dir,
                package_sha,
                manifest,
            )
        except Exception:
            # Isolate the failure to this extension. An exception escaping here
            # aborts the reconcile before it writes the store, leaving every
            # later extension unreconciled.
            logger.exception("local extension reconcile failed for %s", extension_id)
            continue
        data["extensions"][extension_id] = refreshed
        recovered.extend(
            item for item in _recover_quarantined_cohort_for_generation(
                data, extension_id, record, refreshed
            )
            if item not in recovered
        )
        try:
            from extension_backend_loader import evict_persistent_backend
            evict_persistent_backend(extension_id)
        except Exception:
            pass
        changed = True
    return changed, recovered



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
    default_repo_root = _repo_root()
    configured_repo_root = _required_marketplace_repo_root()
    deleted = set((data.get("deleted_extensions") or {}).keys())
    for extension_id, extension_path in _PUBLIC_EXTENSION_PATHS.items():
        if extension_id in deleted:
            continue
        repo_root = configured_repo_root
        if repo_root is None or not (repo_root / extension_path).exists():
            repo_root = default_repo_root
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
        except Exception as exc:
            # Isolate the failure to this extension. An exception escaping here
            # aborts the reconcile before it writes the store, leaving every
            # later extension unreconciled.
            logger.exception("public extension install failed for %s", extension_id)
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



def is_builtin_feature_enabled(extension_id: str) -> bool:
    data = _load()
    record = data["extensions"].get(extension_id)
    if not record:
        return False
    return _record_active(record)


def is_builtin_feature_enabled_cached(extension_id: str | None) -> bool:
    if not extension_id:
        return False
    fingerprint = (store_fingerprint(), installation_profile.integrations_enabled())
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
    return _record_runtime_ready_verified(record)


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
    return (
        installation_profile.integrations_enabled()
        and record.get("enabled") is True
        and _entitlement_active(record.get("entitlement") or {})
    )


def runtime_package_root_for_record(record: dict[str, Any]) -> Path | None:
    source = record.get("source") or {}
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


def supervisor_daemon_package_root(extension_id: str, extension_root: Path) -> Path:
    if extension_id == BUILTIN_SWITCH_CONTROL_EXTENSION_ID:
        return _repo_root() / "switch_control_daemon"
    return extension_root


def _record_runtime_ready(record: dict[str, Any]) -> bool:
    return _record_runtime_ready_verified(record)


def _record_runtime_ready_projected(record: dict[str, Any]) -> bool:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    with _RUNTIME_READY_PROJECTION_LOCK:
        return _RUNTIME_READY_PROJECTION.get(extension_id, False)


def refresh_runtime_readiness_projection() -> dict[str, bool]:
    records = list_extensions()
    refreshed: dict[str, bool] = {}
    fingerprints: dict[str, str] = {}
    for record in records:
        extension_id = str((record.get("manifest") or {}).get("id") or "")
        ready = _record_active(record) and _record_runtime_ready_verified(record)
        fingerprint = _runtime_package_fingerprint(record) if ready else None
        with _RUNTIME_READY_PROJECTION_LOCK:
            previous = _RUNTIME_PACKAGE_FINGERPRINTS.get(extension_id)
        if previous is not None and fingerprint != previous:
            ready = False
        refreshed[extension_id] = ready
        if fingerprint is not None:
            fingerprints[extension_id] = fingerprint
    with _RUNTIME_READY_PROJECTION_LOCK:
        _RUNTIME_READY_PROJECTION.clear()
        _RUNTIME_READY_PROJECTION.update(refreshed)
        for extension_id, fingerprint in fingerprints.items():
            _RUNTIME_PACKAGE_FINGERPRINTS.setdefault(extension_id, fingerprint)
    return dict(refreshed)


def _runtime_package_fingerprint(record: dict[str, Any]) -> str | None:
    manifest = record.get("manifest") or {}
    protocol = _validate_protocol(manifest.get("protocol"))
    entrypoints = manifest.get("entrypoints") or {}
    relative_paths = set(protocol["smoke_test"].get("required_paths") or [])
    backend_path = str(entrypoints.get("backend") or "")
    if backend_path:
        relative_paths.add(backend_path)
    static_modules = _smoke_static_modules(entrypoints)
    modules = set(protocol["smoke_test"].get("python_modules") or [])
    modules.update(_required_smoke_python_modules(entrypoints))
    root = Path(str((record.get("source") or {}).get("install_path") or "")).resolve()
    daemon_root = supervisor_daemon_package_root(str(manifest.get("id") or ""), root)
    if daemon_root != root:
        modules.difference_update(
            str(item.get("module") or "") for item in entrypoints.get("daemons") or []
        )
    for module in modules:
        static_path = static_modules.get(module)
        if static_path:
            relative_paths.add(static_path)
            continue
        module_path = Path(*module.split("."))
        candidates = (module_path.with_suffix(".py"), module_path / "__init__.py")
        match = next((candidate for candidate in candidates if (root / candidate).is_file()), None)
        if match is None:
            return None
        relative_paths.add(str(match))
    if not relative_paths:
        return ""
    digest = hashlib.sha256()
    try:
        for rel_path in sorted(relative_paths):
            path = (root / rel_path).resolve()
            if not path.is_relative_to(root):
                return None
            candidates = [path]
            if path.is_dir():
                candidates = sorted(item for item in path.rglob("*") if item.is_file())
            for candidate in candidates:
                digest.update(str(candidate.relative_to(root)).encode("utf-8"))
                with candidate.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _record_runtime_ready_verified(record: dict[str, Any]) -> bool:
    if not _record_backend_surface_ready(record):
        return False
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    if extension_id in {BUILTIN_TODOS_EXTENSION_ID, MARKETPLACE_EXTENSION_ID}:
        return True
    task_keys = extension_provisioned_internal_llm_tasks(record)
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
        for extension_id in _PUBLIC_EXTENSION_PATHS
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
    for item in manifest["entrypoints"]["harness_profiles"]:
        path = (package_dir / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ExtensionError("harness profile path escapes extension package")
        if not path.exists() or not path.is_file():
            raise ExtensionError(f"harness profile file not found: {item['path']}")
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
    ready_marker = "BETTER_AGENT_SMOKE_READY"
    code = (
        "import importlib, importlib.util, json, os, py_compile, sys\n"
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
        "        if not origin or origin in {'built-in', 'namespace'}:\n"
        "            raise RuntimeError(f'package module has no file origin: {module}')\n"
        "        path = Path(origin).resolve()\n"
        "        if not path.is_relative_to(root):\n"
        "            raise RuntimeError(f'package module resolves outside package: {module}')\n"
        "        py_compile.compile(str(path), doraise=True)\n"
        "        continue\n"
        "    importlib.import_module(module)\n"
        f"sys.stdout.write({ready_marker!r} + '\\n')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
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
        timeout=_SMOKE_IMPORT_OWNER_DEADLINE_SECONDS,
        env=_smoke_subprocess_env(python_path_parts),
    )
    if result.returncode != 0:
        detail = _scrub((result.stderr or result.stdout or "module import failed").strip())
        raise ExtensionError(f"protocol.smoke_test.python_modules failed: {detail}")
    if ready_marker not in (result.stdout or "").splitlines():
        raise ExtensionError("protocol.smoke_test.python_modules failed: readiness not acknowledged")


def _run_extension_smoke_test(manifest: dict[str, Any], package_dir: Path) -> dict[str, Any]:
    protocol = _validate_protocol(manifest.get("protocol"))
    _validate_protocol_coverage({**manifest, "protocol": protocol})
    smoke = protocol["smoke_test"]
    required_paths = list(smoke.get("required_paths") or [])
    python_modules = list(smoke.get("python_modules") or [])
    for rel_path in required_paths:
        _require_smoke_path(package_dir, rel_path)
    extension_id = str(manifest.get("id") or "")
    daemon_root = supervisor_daemon_package_root(extension_id, package_dir)
    daemon_modules = {
        str(item.get("module") or "") for item in (manifest.get("entrypoints") or {}).get("daemons") or []
    }
    extension_modules = [
        module for module in python_modules if daemon_root == package_dir or module not in daemon_modules
    ]
    _run_python_module_smoke(
        package_dir,
        extension_modules,
        static_modules=_smoke_static_modules(manifest.get("entrypoints") or {}),
    )
    if daemon_root != package_dir:
        _run_python_module_smoke(
            daemon_root,
            [module for module in python_modules if module in daemon_modules],
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


def extension_id_for_mcp_replacement(name: str) -> str | None:
    target = str(name or "").strip()
    if not target:
        return None
    for extension_id, record in (_load().get("extensions") or {}).items():
        manifest = record.get("manifest") if isinstance(record, dict) else None
        entrypoints = (manifest or {}).get("entrypoints") if isinstance(manifest, dict) else None
        for item in (entrypoints or {}).get("mcp") or []:
            if isinstance(item, dict) and item.get("replaces_builtin") == target:
                return str(extension_id)
    return None


def extension_id_for_role(role: str) -> str | None:
    clean = str(role or "").strip()
    if clean not in CORE_ROLES:
        raise ExtensionError(f"Unknown core role: {clean}")
    return core_role_owners().get(clean)


def core_role_owners() -> MappingProxyType:
    global _CORE_ROLE_OWNERS_CACHE
    fingerprint = store_fingerprint()
    with _CORE_ROLE_OWNERS_LOCK:
        cached = _CORE_ROLE_OWNERS_CACHE
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    owners: dict[str, str] = {}
    for extension_id, record in (_load().get("extensions") or {}).items():
        if not isinstance(record, dict) or not _record_active(record):
            continue
        for role in ((record.get("manifest") or {}).get("core_roles") or []):
            if role not in CORE_ROLES:
                continue
            owner = owners.get(role)
            if owner is not None and owner != extension_id:
                raise ExtensionError(f"core role {role!r} is declared by multiple active extensions")
            owners[role] = str(extension_id)
    projection = MappingProxyType(owners)
    final_fingerprint = _refresh_store_fingerprint_cache()
    if final_fingerprint != fingerprint:
        return core_role_owners()
    with _CORE_ROLE_OWNERS_LOCK:
        _CORE_ROLE_OWNERS_CACHE = (final_fingerprint, projection)
    return projection


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


def needs_identity_token(record: dict[str, Any]) -> bool:
    permissions = declared_permissions(record)
    return has_permission(record, "internal_loopback") or bool(permissions.get("capabilities"))


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
    the release (``better_agent_bundled``), sourced from its installed package on
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


@contextmanager
def _verified_artifact_package(
    *,
    artifact_url: str,
    artifact_sha256: str,
    artifact_signature: str,
    expected_extension_id: str = "",
    expected_version: str = "",
) -> Any:
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
        _verify_artifact_signature(
            extension_id=manifest["id"],
            version=manifest["version"],
            artifact_sha256=artifact_sha256,
            signature=artifact_signature,
        )
        yield package_dir, manifest, artifact_url, artifact_sha256


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
    with _verified_artifact_package(
        artifact_url=artifact_url,
        artifact_sha256=artifact_sha256,
        artifact_signature=artifact_signature,
        expected_extension_id=expected_extension_id,
        expected_version=expected_version,
    ) as (package_dir, manifest, clean_artifact_url, clean_artifact_sha256):
        existing = _load()["extensions"].get(manifest["id"]) if persist else None
        return _install_from_package_dir(
            package_dir=package_dir,
            source={
                "type": source_type,
                "repo_url": clean_artifact_url,
                "extension_path": "",
                "ref": "",
                "commit_sha": clean_artifact_sha256,
                "artifact_sha256": clean_artifact_sha256,
                "artifact_url": clean_artifact_url,
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


def preview_marketplace_metadata(*, metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ExtensionError("marketplace metadata is required")
    with _verified_artifact_package(
        artifact_url=str(metadata.get("artifact_url") or ""),
        artifact_sha256=str(metadata.get("artifact_sha256") or ""),
        artifact_signature=str(metadata.get("signature") or metadata.get("artifact_signature") or ""),
        expected_extension_id=str(metadata.get("extension_id") or metadata.get("id") or ""),
        expected_version=str(metadata.get("version") or ""),
    ) as (_, manifest, _, _):
        return copy.deepcopy(manifest)


def prepare_marketplace_install(extension_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    clean_id = str(extension_id or "").strip()
    if not _ID_RE.fullmatch(clean_id):
        raise ExtensionError("extension_id is invalid")
    metadata_id = str(metadata.get("extension_id") or metadata.get("id") or "").strip()
    if metadata_id != clean_id:
        raise ExtensionError("marketplace metadata extension id does not match request")
    manifest = preview_marketplace_metadata(metadata=metadata)
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _MARKETPLACE_PREVIEWS_LOCK:
        expired = [key for key, (expires, _, _) in _MARKETPLACE_PREVIEWS.items() if expires <= now]
        for key in expired:
            _MARKETPLACE_PREVIEWS.pop(key, None)
        if len(_MARKETPLACE_PREVIEWS) >= _MAX_MARKETPLACE_PREVIEWS:
            oldest = min(_MARKETPLACE_PREVIEWS, key=lambda key: _MARKETPLACE_PREVIEWS[key][0])
            _MARKETPLACE_PREVIEWS.pop(oldest, None)
        _MARKETPLACE_PREVIEWS[token] = (
            now + _MARKETPLACE_PREVIEW_TTL_SECONDS,
            clean_id,
            copy.deepcopy(metadata),
        )
    return {"manifest": manifest, "preview_token": token}


def install_marketplace_preview(
    extension_id: str,
    preview_token: str,
    *,
    entitlement_token: str = "",
) -> dict[str, Any]:
    clean_id = str(extension_id or "").strip()
    clean_token = str(preview_token or "").strip()
    with _MARKETPLACE_PREVIEWS_LOCK:
        preview = _MARKETPLACE_PREVIEWS.pop(clean_token, None)
    if preview is None:
        raise ExtensionError("marketplace preview is invalid or expired")
    expires_at, preview_extension_id, metadata = preview
    if expires_at <= time.monotonic() or preview_extension_id != clean_id:
        raise ExtensionError("marketplace preview is invalid or expired")
    return install_from_artifact(
        artifact_url=str(metadata.get("artifact_url") or ""),
        artifact_sha256=str(metadata.get("artifact_sha256") or ""),
        artifact_signature=str(metadata.get("signature") or metadata.get("artifact_signature") or ""),
        entitlement_token=entitlement_token,
        expected_extension_id=clean_id,
        expected_version=str(metadata.get("version") or ""),
        source_type="marketplace",
        metadata_url=marketplace_metadata_url(clean_id),
    )


def apply_marketplace_update_metadata(
    extension_id: str,
    metadata: dict[str, Any],
    *,
    expected_version: str,
) -> dict[str, Any]:
    clean_id = str(extension_id or "").strip()
    clean_version = str(expected_version or "").strip()
    if not _ID_RE.fullmatch(clean_id):
        raise ExtensionError("extension_id is invalid")
    if not _VERSION_RE.fullmatch(clean_version):
        raise ExtensionError("expected_version is invalid")
    record = require_extension_source(clean_id, "marketplace")
    metadata_id = str(metadata.get("extension_id") or metadata.get("id") or "").strip()
    metadata_version = str(metadata.get("version") or "").strip()
    if metadata_id != clean_id or metadata_version != clean_version:
        raise ExtensionError("marketplace update target does not match approved action")
    installed_version = str((record.get("manifest") or {}).get("version") or "")
    if installed_version == clean_version:
        return {
            "extension_id": clean_id,
            "source_type": "marketplace",
            "updated": False,
            "skipped": "up_to_date",
        }
    updated = install_from_marketplace_metadata(
        metadata=metadata,
        source_type="marketplace",
    )
    _drop_update_cache_row(clean_id)
    return {
        "extension_id": clean_id,
        "source_type": "marketplace",
        "updated": True,
        "version": updated["manifest"].get("version", ""),
    }


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


_UPDATABLE_SOURCE_TYPES = frozenset({"git", "marketplace", "better_agent_signed"})

_updates_cache_lock = threading.Lock()
_updates_cache: dict[str, Any] | None = None


def _drop_update_cache_row(extension_id: str) -> None:
    global _updates_cache
    with _updates_cache_lock:
        if _updates_cache is None:
            return
        rows = [
            row for row in _updates_cache.get("results", [])
            if row.get("extension_id") != extension_id
        ]
        _updates_cache = {
            **_updates_cache,
            "results": rows,
            "available": [r["extension_id"] for r in rows if r.get("update_available")],
        }


def cached_extension_updates() -> dict[str, Any] | None:
    with _updates_cache_lock:
        return copy.deepcopy(_updates_cache)


def _check_git_update(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source") or {}
    repo_url = _validate_repo_url(str(source.get("repo_url") or ""))
    ref = str(source.get("ref") or "").strip()
    remote_commit = _git_remote_commit(repo_url, ref)
    installed_commit = str(source.get("commit_sha") or "").strip()
    return {
        "update_available": bool(remote_commit and installed_commit != remote_commit),
        "available_version": "",
        "available_sha": remote_commit,
    }


def _check_marketplace_update(extension_id: str, record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source") or {}
    metadata = _fetch_json(_marketplace_metadata_url_for_record(extension_id, record))
    published_sha = str(metadata.get("artifact_sha256") or "").strip().lower()
    installed_sha = str(source.get("artifact_sha256") or source.get("commit_sha") or "").strip().lower()
    return {
        "update_available": bool(published_sha and installed_sha != published_sha),
        "available_version": str(metadata.get("version") or ""),
        "available_sha": published_sha,
    }


def check_extension_updates(*, refresh: bool = False) -> dict[str, Any]:
    """Check-only update scan for remote-sourced extensions.

    The result is a disposable cached projection; installed records stay the
    source of truth. Per-extension check failures land in the row's "error"
    field instead of failing the whole scan, so one unreachable source cannot
    hide the others.
    """
    global _updates_cache
    if not refresh:
        cached = cached_extension_updates()
        if cached is not None:
            return cached
    data = _load()
    results: list[dict[str, Any]] = []
    for extension_id, record in sorted(data["extensions"].items()):
        source_type = str((record.get("source") or {}).get("type") or "")
        if source_type not in _UPDATABLE_SOURCE_TYPES:
            continue
        row: dict[str, Any] = {
            "extension_id": extension_id,
            "source_type": source_type,
            "installed_version": str((record.get("manifest") or {}).get("version") or ""),
        }
        try:
            if source_type == "git":
                row.update(_check_git_update(record))
            else:
                row.update(_check_marketplace_update(extension_id, record))
        except ExtensionError as exc:
            row.update({"update_available": False, "error": str(exc)})
        results.append(row)
    snapshot = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "available": [row["extension_id"] for row in results if row["update_available"]],
    }
    with _updates_cache_lock:
        _updates_cache = copy.deepcopy(snapshot)
    return snapshot


def apply_extension_update(extension_id: str) -> dict[str, Any]:
    clean_id = str(extension_id or "").strip()
    if not _ID_RE.fullmatch(clean_id):
        raise ExtensionError("extension_id is invalid")
    data = _load()
    record = data["extensions"].get(clean_id)
    if not record:
        raise ExtensionError("Extension not installed")
    source_type = str((record.get("source") or {}).get("type") or "")
    if source_type not in _UPDATABLE_SOURCE_TYPES:
        raise ExtensionError("Extension source does not support remote updates")
    if source_type == "git":
        updated = _update_git_extension(clean_id, record)
    else:
        updated = _update_marketplace_extension(clean_id, record)
    if updated is None:
        # The source is already current; clear any stale "available" row.
        _drop_update_cache_row(clean_id)
        return {
            "extension_id": clean_id,
            "source_type": source_type,
            "updated": False,
            "skipped": "up_to_date",
        }
    return {
        "extension_id": clean_id,
        "source_type": source_type,
        "updated": True,
        "version": updated["manifest"].get("version", ""),
    }


def _require_extension_source(
    data: dict[str, Any],
    extension_id: str,
    required_source_type: str = "",
) -> dict[str, Any]:
    record = data["extensions"].get(extension_id)
    if not record:
        raise ExtensionError("Extension not installed")
    if required_source_type and str((record.get("source") or {}).get("type") or "") != required_source_type:
        raise ExtensionError(f"Extension is not managed by {required_source_type}")
    return record


def require_extension_source(extension_id: str, required_source_type: str) -> dict[str, Any]:
    return copy.deepcopy(_require_extension_source(_load(), extension_id, required_source_type))


def set_enabled(
    extension_id: str,
    enabled: bool,
    *,
    required_source_type: str = "",
) -> dict[str, Any]:
    data = _load()
    record = _require_extension_source(data, extension_id, required_source_type)
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
    _invalidate_pending_health_decisions(data, extension_id)
    record["enabled"] = bool(enabled)
    _rotate_activation_identity(record)
    if enabled:
        record.pop("quarantine", None)
        record.pop("slow_backend_calls", None)
    elif record.get("quarantine"):
        record.pop("quarantine", None)
    record["updated_at"] = _now()
    _save(data)
    _evict_extension_backend(extension_id)
    extension_applied_config.reconcile(record)
    reconcile_runtime_skills()
    import extension_token_registry
    if bool(enabled):
        if needs_identity_token(record):
            extension_token_registry.mint(extension_id)
    else:
        # Revoke so a disabled extension's token stops authenticating immediately.
        extension_token_registry.revoke(extension_id)
    return record


def _dependent_cohort(
    data: dict[str, Any], extension_id: str
) -> list[str]:
    cohort = {extension_id}
    changed = True
    while changed:
        changed = False
        for candidate_id, candidate in data["extensions"].items():
            dependencies = (candidate.get("manifest") or {}).get("dependencies", [])
            if (
                candidate_id not in cohort
                and candidate.get("enabled") is True
                and cohort.intersection(dependencies)
            ):
                if candidate_id in REQUIRED_EXTENSION_IDS:
                    return []
                cohort.add(candidate_id)
                changed = True
    return sorted(cohort)


def _health_decision_cohort(
    data: dict[str, Any], cohort: list[str]
) -> list[dict[str, Any]]:
    return [
        {
            "extension_id": extension_id,
            "activation_id": str(
                data["extensions"][extension_id].get("activation_id") or ""
            ),
            "generation": _record_generation(data["extensions"][extension_id]),
            "dependencies": sorted(
                str(item)
                for item in (
                    (data["extensions"][extension_id].get("manifest") or {}).get(
                        "dependencies"
                    )
                    or []
                )
            ),
        }
        for extension_id in cohort
    ]


def _invalidate_pending_health_decisions(
    data: dict[str, Any], extension_id: str
) -> bool:
    changed = False
    for record in data["extensions"].values():
        decision = record.get("pending_health_decision")
        if not isinstance(decision, dict):
            continue
        members = decision.get("cohort") or []
        if any(
            isinstance(item, dict) and item.get("extension_id") == extension_id
            for item in members
        ):
            record.pop("pending_health_decision", None)
            record["updated_at"] = _now()
            changed = True
    return changed


def _record_backend_incident(
    extension_id: str,
    *,
    activation_id: str,
    elapsed_seconds: float,
    history_key: str,
    reason: str,
    route_path: str | None = None,
    incident_id: str | None = None,
    node_id: str | None = None,
    occurred_at: float | None = None,
) -> list[str]:
    # One threshold per incident kind: an incident tied to a backend route is
    # judged by that route's declared budget (needs the record, so it is checked
    # under the lock below); an incident with no route uses the global floor.
    if route_path is None and elapsed_seconds < EXTENSION_SLOW_CALL_SECONDS:
        return []
    received_at = time.time()
    observed_at = float(occurred_at) if occurred_at is not None else received_at
    if incident_id or node_id:
        if (
            not incident_id
            or not node_id
            or not re.fullmatch(r"[a-f0-9]{32}", incident_id)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", node_id)
            or observed_at
            > received_at + _EXTENSION_INCIDENT_FUTURE_SKEW_SECONDS
            or observed_at
            < received_at - _EXTENSION_SLOW_CALL_WINDOW_SECONDS
        ):
            return []
        observed_at = min(observed_at, received_at)
    cutoff = received_at - _EXTENSION_SLOW_CALL_WINDOW_SECONDS
    with _store_lock():
        data = _read_store_unlocked()
        record = data["extensions"].get(extension_id)
        if (
            not record
            or record.get("enabled") is not True
            or not activation_id
            or record.get("activation_id") != activation_id
            or extension_id in REQUIRED_EXTENSION_IDS
        ):
            return []
        if route_path is not None and elapsed_seconds < _record_slow_call_threshold(
            record, route_path
        ):
            return []
        history = read_json(_slow_calls_path(), {"extensions": {}})
        if incident_id and node_id:
            processed_by_node = history.get("processed_incidents")
            if not isinstance(processed_by_node, dict):
                processed_by_node = {}
                history["processed_incidents"] = processed_by_node
            processed = processed_by_node.get(node_id)
            if not isinstance(processed, dict):
                processed = {}
            processed = {
                key: timestamp
                for key, timestamp in processed.items()
                if isinstance(timestamp, (int, float))
                and float(timestamp) + _EXTENSION_INCIDENT_DEDUP_SECONDS
                >= received_at
            }
            if incident_id in processed:
                return []
            if len(processed) >= _EXTENSION_INCIDENT_IDS_PER_NODE:
                processed_by_node[node_id] = processed
                write_json(_slow_calls_path(), history)
                return []
            processed[incident_id] = observed_at
            processed_by_node[node_id] = processed
        histories = history.get("extensions")
        if not isinstance(histories, dict):
            histories = {}
            history = {"extensions": histories}
        extension_histories = histories.get(extension_id)
        if (
            not isinstance(extension_histories, dict)
            or extension_histories.get("activation_id") != activation_id
        ):
            extension_histories = {"activation_id": activation_id}
        incidents = []
        for item in extension_histories.get(history_key, []):
            item_at = item.get("at") if isinstance(item, dict) else item
            if not isinstance(item_at, (int, float)) or float(item_at) < cutoff:
                continue
            incidents.append(item)
        if incident_id and node_id:
            incidents.append({
                "at": observed_at,
                "incident_id": str(incident_id),
                "node_id": str(node_id),
            })
        else:
            incidents.append(observed_at)
        extension_histories[history_key] = incidents
        histories[extension_id] = extension_histories
        write_json(_slow_calls_path(), history)
        if len(incidents) < _EXTENSION_SLOW_CALL_LIMIT:
            return []
        if record.get("pending_health_decision"):
            histories.pop(extension_id, None)
            write_json(_slow_calls_path(), history)
            return []
        cohort = _dependent_cohort(data, extension_id)
        if not cohort:
            return []
        now = _now()
        attributed_generation = _record_generation(data["extensions"][extension_id])
        record["pending_health_decision"] = {
            "id": uuid.uuid4().hex,
            "reason": reason,
            "at": now,
            "attributed_extension_id": extension_id,
            "attributed_generation": attributed_generation,
            "cohort": _health_decision_cohort(data, cohort),
            "elapsed_seconds": round(float(elapsed_seconds), 3),
        }
        record["updated_at"] = now
        _write_store_unlocked(data)
        histories.pop(extension_id, None)
        write_json(_slow_calls_path(), history)
    return cohort


def resolve_health_decision(
    extension_id: str, *, decision_id: str, action: str
) -> dict[str, Any]:
    if action not in {"disable", "keep_enabled"}:
        raise ExtensionError("Health decision action must be disable or keep_enabled")
    affected: list[str] = []
    with _store_lock():
        data = _read_store_unlocked()
        record = _require_extension_source(data, extension_id)
        decision = record.get("pending_health_decision")
        if not isinstance(decision, dict) or decision.get("id") != decision_id:
            raise ExtensionError("Extension health decision is stale")
        expected = decision.get("cohort")
        if not isinstance(expected, list):
            raise ExtensionError("Extension health decision is invalid")
        expected_ids = [
            str(item.get("extension_id") or "")
            for item in expected
            if isinstance(item, dict)
        ]
        current_ids = _dependent_cohort(data, extension_id)
        if not current_ids or current_ids != expected_ids:
            raise ExtensionError("Extension health decision cohort changed")
        if _health_decision_cohort(data, current_ids) != expected:
            raise ExtensionError("Extension health decision cohort changed")
        record.pop("pending_health_decision", None)
        now = _now()
        record["last_health_decision"] = {
            "id": decision_id,
            "action": action,
            "at": now,
            "incident": copy.deepcopy(decision),
        }
        record["updated_at"] = now
        if action == "disable":
            affected = current_ids
            for candidate_id in affected:
                candidate = data["extensions"][candidate_id]
                candidate["enabled"] = False
                candidate.pop("quarantine", None)
                _rotate_activation_identity(candidate)
                candidate["updated_at"] = now
        _write_store_unlocked(data)
    result = copy.deepcopy(get_extension(extension_id) or {})
    projection_errors = reconcile_extension_runtime_state(affected) if affected else []
    if projection_errors:
        result["projection_errors"] = projection_errors
    return result


def reconcile_extension_runtime_state(
    extension_ids: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for extension_id in extension_ids or []:
        try:
            _evict_extension_backend(extension_id)
        except Exception as exc:
            errors.append(f"backend:{extension_id}:{exc}")
    for name, reconcile in (
        ("applied_config", reconcile_all_instructions),
        ("runtime_skills", reconcile_runtime_skills),
        ("tokens", reconcile_extension_tokens),
    ):
        try:
            reconcile()
        except Exception as exc:
            errors.append(f"{name}:{exc}")
    return errors


def record_slow_backend_call(
    extension_id: str,
    *,
    activation_id: str,
    elapsed_seconds: float,
    path: str,
    incident_id: str | None = None,
    node_id: str | None = None,
    occurred_at: float | None = None,
) -> list[str]:
    """Count one slow in-extension call, requesting a user decision after the third one.

    "Slow" is ``path``'s manifest-declared ``backend_timeouts`` budget when it
    declares one — a route the host is willing to wait 360s for is not slow at
    40s — and ``EXTENSION_SLOW_CALL_SECONDS`` for routes with no declaration.
    ``path`` is required: every call arrives through one backend route, and an
    unattributed incident could only be judged against some other route's budget.
    """
    return _record_backend_incident(
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        history_key="slow_asgi",
        reason="repeated_slow_backend_calls",
        route_path=str(path),
        incident_id=incident_id,
        node_id=node_id,
        occurred_at=occurred_at,
    )


def record_backend_timeout(
    extension_id: str,
    *,
    activation_id: str,
    elapsed_seconds: float,
    incident_id: str | None = None,
    node_id: str | None = None,
    occurred_at: float | None = None,
) -> list[str]:
    """Count one host-side timeout. The declared budget is deliberately not
    consulted: the host only times a call out after that budget already elapsed,
    so the incident is by construction outside what the manifest asked for."""
    return _record_backend_incident(
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        history_key="timeout",
        reason="repeated_backend_timeouts",
        incident_id=incident_id,
        node_id=node_id,
        occurred_at=occurred_at,
    )


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
    return record


def reconcile_all_instructions() -> None:
    """Reconcile applied config for every installed extension on startup.

    Instruction content is resolved per session/turn through temporal harness
    profiles, so there are no on-disk instruction blocks to self-heal; this hook
    only re-applies extension applied-config state.
    """
    extension_applied_config.reconcile_all()


def reconcile_runtime_skills() -> int:
    data = _load()
    settings = _load_ext_settings()
    root = Path.home() / ".agents" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    active_native_skill_names: dict[str, str] = {}
    for record in _active_records_from_data(data):
        manifest = record["manifest"]
        for item in manifest.get("entrypoints", {}).get("skills") or []:
            if not is_runtime_skill_enabled(
                manifest["id"], item["name"], settings=settings, record=record
            ):
                continue
            if not native_harness_exposed(
                manifest["id"], "skill", item["name"], settings=settings, record=record
            ):
                continue
            name = item["name"]
            existing_owner = active_native_skill_names.get(name)
            if existing_owner and existing_owner != manifest["id"]:
                raise ExtensionError(
                    f"Native skill name {name!r} is already exposed by {existing_owner}"
                )
            active_native_skill_names[name] = manifest["id"]
    for name, extension_id in active_native_skill_names.items():
        _assert_runtime_skill_target_available(root / name, extension_id)
    removed = _purge_extension_runtime_skills(root, active_native_skill_names)
    installed = 0
    for record in _active_records_from_data(data):
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        manifest = record["manifest"]
        extension_id = manifest["id"]
        for item in manifest.get("entrypoints", {}).get("skills") or []:
            if not is_runtime_skill_enabled(
                extension_id, item["name"], settings=settings, record=record
            ):
                continue
            if not native_harness_exposed(
                extension_id, "skill", item["name"], settings=settings, record=record
            ):
                continue
            source = (install_root / item["path"]).resolve()
            if not source.is_relative_to(install_root):
                continue
            if not source.is_dir() or not (source / "SKILL.md").is_file():
                continue
            target = root / item["name"]
            if _runtime_skill_owner(target) == extension_id and (target / "SKILL.md").is_file():
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
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        for item in manifest.get("entrypoints", {}).get("skills") or []:
            if not is_runtime_skill_enabled(
                manifest["id"], item["name"], settings=settings, record=record
            ):
                continue
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


def runtime_agent_entries() -> list[dict[str, str]]:
    """Subagent definitions from active extensions, resolved to absolute
    per-provider source files. Each entry: {"name": ..., "<provider>": <abspath>}
    for every provider whose source file exists."""
    global _RUNTIME_AGENT_ENTRIES_CACHE
    while True:
        fingerprint = store_fingerprint()
        with _RUNTIME_AGENT_ENTRIES_LOCK:
            cached = _RUNTIME_AGENT_ENTRIES_CACHE
            if cached is not None and cached[0] == fingerprint:
                return [dict(entry) for entry in cached[1]]
        data = read_json(_store_path(), _blank_store())
        if (
            data.get("schema_version") != STORE_SCHEMA_VERSION
            or not isinstance(data.get("extensions"), dict)
            or not isinstance(data.get("deleted_extensions"), dict)
        ):
            raise ExtensionError("extension store snapshot is invalid")
        agents: list[dict[str, str]] = []
        for record in _active_records_from_data(data):
            manifest = record["manifest"]
            install_root = runtime_package_root_for_record(record)
            if install_root is None or not install_root.exists():
                continue
            for item in manifest.get("entrypoints", {}).get("agents") or []:
                resolved: dict[str, str] = {"name": item["name"]}
                for provider_id, rel_path in (item.get("providers") or {}).items():
                    source = (install_root / rel_path).resolve()
                    if not source.is_relative_to(install_root) or not source.is_file():
                        continue
                    resolved[provider_id] = str(source)
                if len(resolved) > 1:
                    agents.append(resolved)
        final_fingerprint = _refresh_store_fingerprint_cache()
        if final_fingerprint != fingerprint:
            continue
        frozen = tuple(dict(entry) for entry in agents)
        with _RUNTIME_AGENT_ENTRIES_LOCK:
            _RUNTIME_AGENT_ENTRIES_CACHE = (final_fingerprint, frozen)
        return [dict(entry) for entry in frozen]


def _active_records_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    if not installation_profile.integrations_enabled():
        return []
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


def _assert_runtime_skill_target_available(target: Path, extension_id: str) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if not target.is_symlink() and _runtime_skill_owner(target) == extension_id:
        return
    raise ExtensionError(f"Native skill name {target.name!r} already exists outside this extension")


def _replace_runtime_skill_dir(source: Path, target: Path, extension_id: str) -> None:
    """Swap ``target`` to a fresh copy of ``source`` without a partial-content window.

    Sessions snapshot this directory concurrently (runtime-skill plugin build,
    codex/agy overlays), so the new tree is staged fully — owner marker
    included — and swapped in with renames; the old tree is removed last.
    """
    _assert_runtime_skill_target_available(target, extension_id)
    staging = target.with_name(f".{target.name}.staging-{os.getpid()}")
    retired = target.with_name(f".{target.name}.retired-{os.getpid()}")
    for leftover in (staging, retired):
        _remove_runtime_skill_path(leftover)
    shutil.copytree(source, staging, symlinks=True)
    (staging / _RUNTIME_SKILL_OWNER_FILE).write_text(extension_id + "\n", encoding="utf-8")
    if target.exists() or target.is_symlink():
        os.rename(target, retired)
    os.rename(staging, target)
    _remove_runtime_skill_path(retired)


def _remove_runtime_skill_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)


def uninstall(extension_id: str, *, required_source_type: str = "") -> None:
    if extension_id in REQUIRED_EXTENSION_IDS:
        raise ExtensionError("Required extension cannot be uninstalled")
    data = _load()
    record = _require_extension_source(data, extension_id, required_source_type)
    data["extensions"].pop(extension_id)
    if extension_id == extension_id_for_role('assistant'):
        import assistant_ui
        assistant_ui.cleanup_singleton()
    _evict_extension_backend(extension_id)
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
    _drop_update_cache_row(extension_id)
    import extension_token_registry
    extension_token_registry.revoke(extension_id)
    reconcile_runtime_skills()
    # Permanent removal, unlike disable (which leaves grants in place so
    # re-enabling restores them without re-approval, as long as the
    # manifest declaration hasn't changed) -- a reinstall must not silently
    # resurrect grants against what could be a completely different package.
    native_mcp_grants.remove_grants_for_extension(extension_id)


def team_definition_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for record in list_extensions():
        extension_id = str((record.get("manifest") or {}).get("id") or "")
        if not is_extension_active(extension_id):
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


def _extra_mcp_server_names(inputs: dict[str, Any]) -> set[str]:
    """Session-scoped opt-in server names from the run inputs — lets a session
    created for a specific job (e.g. a TestApe operator) receive extension MCP
    servers that are default-off for every other session."""
    raw = inputs.get("extra_mcp_servers")
    if not isinstance(raw, list):
        return set()
    return {name for name in (str(item or "").strip() for item in raw) if name}


def _profile_selected_mcp_server_names(inputs: dict[str, Any], extension_id: str) -> set[str]:
    return harness_run_projection.selected_mcp_servers(inputs, extension_id)


def required_profile_mcp_server_names(inputs: dict[str, Any]) -> set[str]:
    projection = harness_run_projection.launcher_projection(inputs)
    if projection.get("extension_selection_authoritative") is not True:
        return set()
    selected_by_extension = projection.get("extension_mcp_servers")
    if not isinstance(selected_by_extension, dict):
        return set()
    required: set[str] = set()
    mapped: set[tuple[str, str]] = set()
    for record in _active_records():
        manifest = record["manifest"]
        extension_id = str(manifest["id"])
        selected = selected_by_extension.get(extension_id)
        if not isinstance(selected, list):
            continue
        selected_names = {
            str(name).strip()
            for name in selected
            if str(name or "").strip()
        }
        for item in _stored_mcp_entrypoints(record):
            item_name = str(item.get("name") or "")
            if item_name not in selected_names:
                continue
            required.add(str(item.get("replaces_builtin") or item_name))
            mapped.add((extension_id, item_name))
    for extension_id, selected in selected_by_extension.items():
        if not isinstance(selected, list):
            continue
        for name in selected:
            item_name = str(name or "").strip()
            if item_name and (str(extension_id), item_name) not in mapped:
                required.add(item_name)
    return required


def _profile_setting_overlays(inputs: dict[str, Any], extension_id: str) -> dict[str, Any]:
    projection = harness_run_projection.launcher_projection(inputs)
    raw = projection.get("extension_setting_overlays") if projection else None
    if not isinstance(raw, dict):
        return {}
    settings = raw.get(extension_id)
    if not isinstance(settings, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in settings.items():
        if isinstance(key, str) and isinstance(item, dict) and "value" in item:
            out[key] = copy.deepcopy(item["value"])
    return out


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
        "user_facing": bool(user_facing),
        "bare_config": bool(bare),
    }
    disabled_extension_ids = _disabled_runtime_extension_ids(inputs)
    granted_native = (
        resolve_native_mcp_servers_for_context(project_path=inputs.get("cwd"))
        if delivery == _HARNESS_DELIVERY_NATIVE
        else {}
    )
    # A resolver-produced harness snapshot is authoritative even when it selects
    # no extension MCPs. Legacy/manual projections without that marker retain
    # the prior non-empty selection behavior.
    launcher_projection = harness_run_projection.launcher_projection(inputs)
    enforce_profile_selection = bool(
        launcher_projection.get("extension_selection_authoritative")
        or harness_run_projection.selected_extension_ids(inputs)
    )
    configs: dict[str, dict[str, Any]] = {}
    for record in _active_records():
        if not _record_runtime_ready(record):
            continue
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        manifest = record["manifest"]
        if manifest["id"] in disabled_extension_ids:
            continue
        for item in _stored_mcp_entrypoints(record):
            profile_selected = item["name"] in _profile_selected_mcp_server_names(
                resolved_inputs,
                manifest["id"],
            )
            server_id = _native_mcp_server_id(item)
            if enforce_profile_selection and not profile_selected:
                continue
            if (
                delivery == _HARNESS_DELIVERY_NATIVE
                and not profile_selected
                and f"{manifest['id']}:{server_id}" not in granted_native
            ):
                continue
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
                if delivery == _HARNESS_DELIVERY_RUNTIME and item.get("user_facing"):
                    config = _apply_mcp_prewarm_daemon(config, server_name, resolved_inputs)
            if config:
                configs[server_name] = config
    return configs


def _apply_mcp_prewarm_daemon(
    config: dict[str, Any] | None,
    server_name: str,
    inputs: dict[str, Any],
) -> dict[str, Any] | None:
    """Swaps a user-facing runtime MCP server's real cold-spawn config
    for the pre-warmed daemon's stub proxy, when `turn_manager` ran the
    prewarm gate for this turn (signaled by `_mcp_prewarm_ready` being
    present in `inputs` at all -- absent means no prewarm was attempted
    for this call, e.g. a direct `runtime_mcp_server_configs()` caller
    outside the turn-dispatch path, so it falls back to today's
    cold-spawn behavior unchanged). A server absent from a ready map was
    not prewarmed and keeps its cold-spawn config. An explicit false/None
    entry means prewarm failed and the server is omitted for that turn.
    """
    if config is None:
        return None
    ready_map = inputs.get("_mcp_prewarm_ready")
    if not isinstance(ready_map, dict):
        return config
    if server_name not in ready_map:
        return config
    ready_value = ready_map[server_name]
    if not ready_value:
        return None
    # `ready_value` is either a bare unix socket-path string (macOS/Linux
    # -- unchanged shape, zero secrets in the stub's env since directory
    # permissions already isolate the socket) or a small dict describing
    # the Windows TCP+secret transport (`supervisor.DaemonReadyResult.
    # ready_map_value`'s single source of truth for this shape).
    if isinstance(ready_value, dict):
        connect_env = dual_env_many({
            "BETTER_CLAUDE_MCP_DAEMON_ADDR": f"{ready_value['host']}:{ready_value['port']}",
            "BETTER_CLAUDE_MCP_DAEMON_CONNECT_SECRET": ready_value["connect_secret"],
        })
    else:
        connect_env = dual_env_many({"BETTER_CLAUDE_MCP_DAEMON_SOCKET": str(ready_value)})
    return {
        **{k: v for k, v in config.items() if k not in ("command", "args", "env")},
        "command": sys.executable,
        "args": ["-m", "mcp_prewarm.stub"],
        "env": {
            **connect_env,
            # The stub is a stdlib-only script but still needs `backend/`
            # (its own package parent) on PYTHONPATH -- unlike the real
            # cold-spawn config this replaces, whose env intentionally
            # carries only the extension's own install/site-packages
            # paths, never the backend dir.
            "PYTHONPATH": str(Path(__file__).resolve().parent),
        },
    }


def runtime_mcp_prewarm_targets(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerates the user-facing runtime MCP items eligible for
    pre-warming for this turn's `inputs` -- same gating
    (`_mcp_item_available_for_inputs`, `user_facing`, module-based
    entrypoint) as `_mcp_server_configs_for_delivery`'s runtime path,
    single source of truth for which items get built each turn. Only
    module entrypoints resolving to a module that declares
    `build_server()` are eligible -- checked via a cheap source-text
    scan (no import) so an extension that doesn't follow the
    convention is silently left on the cold-spawn path instead of
    being wrongly gated to fail-closed unavailable.
    """
    resolved_inputs = {
        **inputs,
        "user_facing": True,
        "bare_config": False,
        # Called from `ClaudeProvider._spawn_run`, BEFORE the runner
        # subprocess's own `hydrate_runner_inputs` bootstrap RPC mints
        # the real session `internal_token` -- at this point in the
        # backend process it is always blank by construction. Only
        # `_mcp_item_available_for_inputs`'s `requires_backend_auth`
        # gate checks this key for truthiness; it is never read as
        # secret material by `_runtime_mcp_server_config_for_item`
        # (which mints its own per-extension token independently), so
        # substituting a non-empty placeholder here just matches the
        # non-empty value the runner is guaranteed to have moments
        # later, instead of prematurely gating every backend-auth
        # extension (e.g. memory) out of pre-warming.
        "internal_token": inputs.get("internal_token") or "pending-runner-bootstrap",
    }
    disabled_extension_ids = _disabled_runtime_extension_ids(inputs)
    targets: list[dict[str, Any]] = []
    for record in _active_records():
        if not _record_runtime_ready(record):
            continue
        install_root = runtime_package_root_for_record(record)
        if install_root is None or not install_root.exists():
            continue
        manifest = record["manifest"]
        if manifest["id"] in disabled_extension_ids:
            continue
        for item in _stored_mcp_entrypoints(record):
            if not item.get("user_facing"):
                continue
            server_name = item.get("replaces_builtin") or item["name"]
            if not _mcp_item_available_for_inputs(record, item, resolved_inputs):
                continue
            real_config = _runtime_mcp_server_config_for_item(record, item, resolved_inputs)
            if not real_config:
                continue
            args = list(real_config.get("args") or [])
            if real_config.get("command") != sys.executable or len(args) < 2 or args[0] != "-m":
                continue
            module_name = args[1]
            if not _module_declares_build_server(module_name):
                continue
            targets.append({
                "extension_id": manifest["id"],
                "server_name": server_name,
                "real_config": real_config,
                "extension_record": record,
            })
    return targets


def _module_declares_build_server(module_name: str) -> bool:
    import importlib.util

    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        return False
    origin = getattr(spec, "origin", None) if spec else None
    if not origin or not os.path.isfile(origin):
        return False
    try:
        source = Path(origin).read_text(encoding="utf-8")
    except OSError:
        return False
    return "def build_server(" in source


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
        "BETTER_CLAUDE_RUNTIME_BROKER": get_env("BETTER_CLAUDE_RUNTIME_BROKER"),
        "BETTER_CLAUDE_APP_SESSION_ID": str(inputs.get("app_session_id") or ""),
        "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
        "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
        "BETTER_CLAUDE_PROVIDER_ID": str(inputs.get("provider_id") or ""),
        "BETTER_CLAUDE_MODE": str(inputs.get("mode") or ""),
        "BETTER_CLAUDE_WORKING_MODE": str(inputs.get("working_mode") or ""),
        "BETTER_CLAUDE_BARE_CONFIG": "1" if inputs.get("bare_config") else "0",
        "BETTER_CLAUDE_USER_FACING": "1"
        if bool(inputs.get("user_facing")) and not bool(inputs.get("bare_config"))
        else "0",
        "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS": ",".join(disabled_extensions),
        "BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS": ",".join(active_capability_ids),
        "BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE": provisioned_tool_profile,
        "BETTER_CLAUDE_HARNESS_LAUNCHER_PROJECTION": json.dumps(
            harness_run_projection.launcher_projection(inputs),
            separators=(",", ":"),
            sort_keys=True,
        ),
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
    profile_selected = server_name in _profile_selected_mcp_server_names(inputs, extension_id)
    if not profile_selected:
        server_id = _native_mcp_server_id(item)
        granted_native = resolve_native_mcp_servers_for_context(project_path=inputs.get("cwd"))
        if f"{extension_id}:{server_id}" not in granted_native:
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
        "BETTER_CLAUDE_RUNTIME_BROKER": get_env("BETTER_CLAUDE_RUNTIME_BROKER"),
        "BETTER_CLAUDE_APP_SESSION_ID": str(inputs.get("app_session_id") or ""),
        "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
        "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
        "BETTER_CLAUDE_PROVIDER_ID": str(inputs.get("provider_id") or ""),
    })
    if (
        manifest["id"] == extension_id_for_role('requirements')
        and str(inputs.get("provisioned_tool_profile") or "").strip() == "requirements_processor"
    ):
        base_env.update(dual_env_many({"BETTER_CLAUDE_REQUIREMENTS_PROCESSOR": "1"}))
    ambient_launch = item.get("ambient_native") is True and not str(
        inputs.get("app_session_id") or ""
    ).strip()
    if (
        needs_identity_token(record)
        and not ambient_launch
        and manifest["id"] not in _BROKERED_MCP_EXTENSION_IDS
    ):
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
    setting_overlays = _profile_setting_overlays(inputs, manifest["id"])
    if setting_overlays:
        env.update(dual_env_many({
            "BETTER_CLAUDE_EXTENSION_SETTING_OVERLAYS": json.dumps(
                setting_overlays,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }))
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
    if sdk_path:
        pythonpath_parts.append(sdk_path)
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    module = str(item.get("module") or "").strip()
    python = _extension_python(
        install_root,
        has_dependency_environment=bool(
            manifest.get("entrypoints", {}).get("python_requirements")
        ),
    )
    if module:
        return {
            "command": str(python),
            "args": ["-m", module, *list(item.get("args") or [])],
            "env": env,
            **timeout_config,
        }
    script = (install_root / item["python"]).resolve()
    if not script.is_relative_to(install_root) or not script.is_file():
        return None
    return {
        "command": str(python),
        "args": [
            "-m",
            "better_agent_sdk.script_entrypoint",
            str(install_root),
            str(script),
            *list(item.get("args") or []),
        ],
        "env": env,
        **timeout_config,
    }


def _mcp_tool_timeout_config(manifest: dict[str, Any], item: dict[str, Any]) -> dict[str, float]:
    if (
        manifest["id"] == extension_id_for_role('requirements')
        and (
            str(item.get("name") or "") == "better-agent-requirements"
            or str(item.get("replaces_builtin") or "") == "get-requirements"
        )
    ):
        return {"tool_timeout_sec": 1380.0}
    return {}


def _mcp_item_available_for_inputs(
    record: dict[str, Any],
    item: dict[str, Any],
    inputs: dict[str, Any],
) -> bool:
    manifest = record["manifest"]
    if not item.get("python") and not item.get("module") and not item.get("command"):
        return False
    session_opted_in = item["name"] in _extra_mcp_server_names(inputs)
    if not session_opted_in and not is_mcp_server_enabled(manifest["id"], item["name"], record=record):
        return False
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("user_facing")) and not bare
    if (
        item.get("user_facing")
        and not user_facing
        and not session_opted_in
        and not (bare and item.get("bare_allowed"))
    ):
        return False
    if bare and not item.get("bare_allowed"):
        return False
    explicit_backend_url = str(inputs.get("backend_url") or "").strip()
    backend_url = str(
        explicit_backend_url
        or get_env("BETTER_CLAUDE_BACKEND_URL")
        or "http://localhost:8000"
    ).strip()
    internal_token = str(inputs.get("internal_token") or "").strip()
    runtime_broker = get_env("BETTER_CLAUDE_RUNTIME_BROKER").strip()
    launcher_can_mint_token = (
        bool(inputs.get("extension_mcp_launcher_context"))
        and bool(str(inputs.get("app_session_id") or "").strip())
        and bool(explicit_backend_url)
    )
    brokered = (
        manifest["id"] in _BROKERED_MCP_EXTENSION_IDS
        and bool(runtime_broker)
    )
    if (
        item.get("requires_backend_auth")
        and not (
            (backend_url and internal_token)
            or brokered
            or launcher_can_mint_token
        )
    ):
        return False
    predicate = item.get("predicate")
    if predicate and not _mcp_predicate_matches(predicate, inputs):
        return False
    return True


def reconcile_extension_tokens() -> int:
    """Make the token registry exactly match active extensions that need tokens."""
    import extension_token_registry
    desired: set[str] = set()
    for record in _active_records():
        if needs_identity_token(record):
            desired.add(str(record["manifest"]["id"]))
    existing = extension_token_registry.extension_ids()
    for extension_id in desired:
        extension_token_registry.mint(extension_id)
    for extension_id in existing - desired:
        extension_token_registry.revoke(extension_id)
    return len(desired)


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


def _native_mcp_server_id(item: dict[str, Any]) -> str:
    """Canonical server_id for a native MCP entrypoint item -- the grant-store
    identity key everywhere a grant is created, matched, or reported against.
    Always ``replaces_builtin`` when set, else the item's own name."""
    return str(item.get("replaces_builtin") or item.get("name") or "").strip()


_NATIVE_MCP_PACKAGE_FINGERPRINT_CACHE: dict[str, tuple[tuple[float, int], str]] = {}
_NATIVE_MCP_PACKAGE_FINGERPRINT_LOCK = threading.Lock()


def _native_mcp_package_walk_targets(install_root: Path) -> list[Path] | None:
    """Every file this fingerprint covers, symlinks resolved and verified to
    stay inside `install_root` (fail closed -- returns None -- on any
    escape, matching `_runtime_package_fingerprint`'s own
    `is_relative_to` check). Includes `.venv`: vendored dependencies run in
    the same process with the same privileges as the extension's own code,
    so an update that swaps a package inside `.venv` and touches nothing
    else must still invalidate the grant. Excludes `__pycache__` directories
    outright (regenerable, Python-version-keyed bytecode, never source of
    record) and a `.pyc`/`.pyo` ONLY when a sibling `.py` exists at the same
    relative path (regenerable in that case) -- a package that ships
    compiled-only modules has no `.py` sibling and that bytecode IS the
    source of record, so it's hashed like any other file."""
    targets: list[Path] = []
    for path in install_root.rglob("*"):
        if "__pycache__" in path.relative_to(install_root).parts:
            continue
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                return None
            if not resolved.is_relative_to(install_root):
                return None
            if not resolved.is_file():
                continue
        elif not path.is_file():
            continue
        if path.suffix in (".pyc", ".pyo") and path.with_suffix(".py").is_file():
            continue
        targets.append(path)
    return targets


def _native_mcp_package_content_fingerprint(record: dict[str, Any]) -> str | None:
    """SHA256 over EVERY file in the installed package tree (relative path +
    content), not just the manifest's declared entry files -- see
    `_native_mcp_package_walk_targets` for exactly what's included/excluded
    and why.

    Deliberately does NOT reuse `_runtime_package_fingerprint` (which only
    hashes smoke-test-declared required_paths/python_modules): a native MCP
    server's implementation can transitively import helper modules the
    manifest never lists as a required smoke-test path, so an entry-file-only
    hash would miss an update that rewrites `helpers.py` while leaving
    `server.py` byte-identical -- the exact escalation this digest exists to
    close. This digest gates a security decision (does an outstanding grant
    still apply), so correctness outweighs cost -- but full content hashing
    on every call is real cost once `.venv` is in scope, so this caches the
    actual hash behind a CHEAP signal (max mtime + file count across exactly
    the same walk) and only re-hashes content when that signal changes."""
    install_root = runtime_package_root_for_record(record)
    if install_root is None:
        return None
    extension_id = str((record.get("manifest") or {}).get("id") or "").strip()
    try:
        targets = _native_mcp_package_walk_targets(install_root)
        if targets is None:
            return None
        signal = (max((p.stat().st_mtime for p in targets), default=0.0), len(targets))
        with _NATIVE_MCP_PACKAGE_FINGERPRINT_LOCK:
            cached = _NATIVE_MCP_PACKAGE_FINGERPRINT_CACHE.get(extension_id)
        if cached is not None and cached[0] == signal:
            return cached[1]
        digest = hashlib.sha256()
        for path in sorted(targets):
            digest.update(str(path.relative_to(install_root)).encode("utf-8"))
            with path.open("rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
        fingerprint = digest.hexdigest()
    except OSError:
        return None
    with _NATIVE_MCP_PACKAGE_FINGERPRINT_LOCK:
        _NATIVE_MCP_PACKAGE_FINGERPRINT_CACHE[extension_id] = (signal, fingerprint)
    return fingerprint


def runtime_package_content_fingerprint(record: dict[str, Any]) -> str | None:
    return _native_mcp_package_content_fingerprint(record)


def native_mcp_declarations(
    record: dict[str, Any],
) -> dict[tuple[str, str], "native_mcp_grants.ServerDeclaration"]:
    """The reviewable declarations for one extension record: every native MCP
    server it exposes (per entrypoints.mcp, ambient/native-eligible items
    only) paired with the scopes permissions.native_mcp declares it
    eligible for. Command/args are always the fixed launcher stub
    (extension_mcp._launcher_command) -- extensions never control the
    literal command; what a grant actually authorizes is "this extension_id
    may expose a native MCP server under this server_id, at these scopes."

    `package_fingerprint` is a whole-package content hash
    (`_native_mcp_package_content_fingerprint`) rather than a manifest
    version string, because version is metadata declared by the same party
    the digest is meant to constrain, and there is no "re-present
    permissions.native_mcp for review" flow on update in this codebase --
    an artifact hash is the only input that actually measures what's
    installed instead of trusting an assertion about it. Fails closed
    (returns no declarations for the whole record) if the fingerprint can't
    be computed rather than letting an unresolvable package degrade into a
    constant in the digest.
    """
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "").strip()
    if not extension_id:
        return {}
    package_fingerprint = _native_mcp_package_content_fingerprint(record)
    if not package_fingerprint:
        return {}
    native_scopes = dict(declared_permissions(record).get("native_mcp") or {})
    declarations: dict[tuple[str, str], native_mcp_grants.ServerDeclaration] = {}
    for item in _stored_mcp_entrypoints(record):
        if not _native_harness_eligible(record, "mcp", item.get("name", "")):
            continue
        server_id = _native_mcp_server_id(item)
        item_name = str(item.get("name") or "").strip()
        if not server_id or not item_name:
            continue
        scopes = native_scopes.get(server_id)
        if not scopes:
            continue
        command, args = extension_mcp._launcher_command(extension_id, item_name)
        env_keys = (extension_mcp._MARKER_EXTENSION_ID, extension_mcp._MARKER_SERVER_NAME)
        declarations[(extension_id, server_id)] = native_mcp_grants.ServerDeclaration(
            command=command, args=tuple(args), env_keys=env_keys, scopes=tuple(scopes),
            package_fingerprint=package_fingerprint,
        )
    return declarations


def _all_native_mcp_declarations() -> dict[tuple[str, str], "native_mcp_grants.ServerDeclaration"]:
    import config_store

    disabled_extension_ids = set(config_store.get_disabled_builtin_extensions())
    declarations: dict[tuple[str, str], native_mcp_grants.ServerDeclaration] = {}
    for record in _active_records():
        extension_id = record["manifest"]["id"]
        if extension_id in disabled_extension_ids or not _record_runtime_ready(record):
            continue
        declarations.update(native_mcp_declarations(record))
    return declarations


def grant_native_mcp_server(
    extension_id: str, server_id: str, scope: str, *, project_path: str | None = None, node_id: str = "primary",
) -> None:
    """Create a grant -- the narrower record that actually makes a
    manifest-declared, install-time-reviewed server resolve at the given
    scope. Only `global` and `project` are reachable here (PR1); `session`
    and `turn` grants are created at runtime via activate_mcp_server, a
    separate, deliberately isolated surface (PR3), not this function."""
    if scope not in ("global", "project"):
        raise ExtensionError(f"grant_native_mcp_server only supports global/project scope, got {scope!r}")
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    declaration = native_mcp_declarations(record).get((extension_id, server_id))
    if declaration is None:
        raise ExtensionError(
            f"{extension_id!r} does not declare a native MCP server {server_id!r} "
            "eligible for native exposure (check entrypoints.mcp and permissions.native_mcp)"
        )
    if scope not in declaration.scopes:
        raise ExtensionError(
            f"{extension_id!r} did not declare {server_id!r} eligible for {scope!r} scope "
            f"(declared: {', '.join(declaration.scopes)})"
        )
    if scope == "project":
        if not project_path:
            raise ExtensionError("project_path is required for project-scope grants")
        target = native_mcp_grants.project_target(node_id, project_path)
        if target is None:
            raise ExtensionError(f"could not resolve project_path {project_path!r}")
    else:
        target = ""
    native_mcp_grants.add_grant(
        extension_id=extension_id, server_id=server_id, scope=scope, target=target,
        digest=declaration.digest(), created_at=_now(),
    )


def revoke_native_mcp_server(
    extension_id: str, server_id: str, scope: str, *, project_path: str | None = None, node_id: str = "primary",
) -> bool:
    if scope == "project":
        if not project_path:
            raise ExtensionError("project_path is required for project-scope revocation")
        target = native_mcp_grants.project_target(node_id, project_path)
        if target is None:
            raise ExtensionError(f"could not resolve project_path {project_path!r}")
    else:
        target = ""
    removed = native_mcp_grants.remove_grant(
        extension_id=extension_id, server_id=server_id, scope=scope, target=target,
    )
    return removed


def resolve_native_mcp_servers_for_context(
    *, node_id: str = "primary", project_path: str | None = None,
    session_id: str | None = None, root_id: str | None = None, turn_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The single call every provider's per-turn/per-run injection point
    (runner.py, runner_codex.py, runner_agy.py) makes. Gathers currently
    active/enabled extensions' declarations and resolves them against the
    grant store for this context -- global grants always included, project
    grants for `project_path`, session/turn grants for `session_id`/
    `(root_id, turn_id)` once PR3 lands any (schema already supports it;
    nothing creates them yet, so those two branches are no-ops until then)."""
    return native_mcp_grants.resolve_native_mcp_servers(
        active_declarations=_all_native_mcp_declarations(),
        node_id=node_id, project_path=project_path,
        session_id=session_id, root_id=root_id, turn_id=turn_id,
    )


def _hook_endpoints(hook_key: str) -> list[tuple[str, str]]:
    """(extension_id, path) for active, runtime-ready INSTALLED extensions
    declaring a ``entrypoints.hooks.<hook_key>`` backend path."""
    out: list[tuple[str, str]] = []
    for record in list_extensions():
        if not _record_active(record):
            continue
        path = (record["manifest"].get("entrypoints") or {}).get("hooks", {}).get(hook_key)
        if not path or not _record_runtime_ready_projected(record):
            continue
        # Mirror the `backend_routes` gate in backend_entrypoint_spec (see line
        # ~4892): a hook is a backend invocation, so an extension whose spec
        # resolves to None would 404 on every fan-out. Filter it here to keep
        # misconfigured extensions off the invocation hot paths.
        if not has_permission(record, "backend_routes"):
            continue
        out.append((record["manifest"]["id"], str(path)))
    return out


def post_turn_hooks() -> list[tuple[str, str]]:
    return _hook_endpoints("post_turn")


def pre_turn_hooks() -> list[tuple[str, str]]:
    return _hook_endpoints("pre_turn")


def session_event_hooks() -> list[tuple[str, str]]:
    return _hook_endpoints("session_event")


def pre_send_advisory_hooks() -> list[tuple[str, str]]:
    return _hook_endpoints("pre_send_advisory")


def provider_transport_hooks() -> list[tuple[str, str]]:
    return _hook_endpoints("provider_transport")


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


def backend_surface_status(extension_id: str) -> str:
    """Classify backend resolution without collapsing unavailability into 404."""
    record = get_extension(extension_id)
    if not record:
        return "absent"
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    if not (entrypoints.get("backend") or entrypoints.get("backend_module")):
        return "no_surface"
    if not has_permission(record, "backend_routes"):
        return "no_surface"
    if not _record_active(record) or not _record_backend_surface_ready(record):
        return "unavailable"
    return "ready"


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
        frontend_modules = [
            item
            for item in manifest.get("entrypoints", {}).get("frontend_modules") or []
            if is_frontend_module_enabled(manifest["id"], item["slot"], item["id"])
        ]
        version = _frontend_asset_version(record)
        entries.append(
            {
                "extension_id": manifest["id"],
                "name": manifest["name"],
                "entrypoint": frontend_path,
                "entrypoint_url": _frontend_asset_url(
                    manifest["id"], frontend_path, version
                ),
                "payments": (manifest.get("permissions") or {}).get("payments") is True,
                "marketplace_auth": (manifest.get("permissions") or {}).get("marketplace_auth") is True,
                "frontend_modules": [
                    {
                        "slot": item["slot"],
                        "id": item["id"],
                        "label": item["label"],
                        "kind": item["kind"],
                        "module": item["module"],
                        "module_url": _frontend_asset_url(
                            manifest["id"], item["module"], version
                        ),
                    }
                    for item in frontend_modules
                ],
            }
        )
    return _projection_cache_put("frontend_entrypoints", key, entries)


def _frontend_entrypoints_cached_for_current_files() -> list[dict[str, Any]] | None:
    fingerprint = store_fingerprint()
    settings_fp = extension_settings_fingerprint()
    integrations_enabled = installation_profile.integrations_enabled()
    for key, value in _projection_cache_items("frontend_entrypoints"):
        if key == (fingerprint, settings_fp, integrations_enabled):
            return value
    return None


def frontend_entrypoints_cache_key() -> tuple[Any, ...]:
    return (
        store_fingerprint(),
        extension_settings_fingerprint(),
        installation_profile.integrations_enabled(),
    )


def _frontend_asset_version(record: dict[str, Any]) -> str:
    return _record_generation(record)


def _frontend_asset_url(extension_id: str, asset_path: str, version: str) -> str:
    """The one place a frontend asset URL is built, cache-bust included."""
    return f"/api/extensions/{extension_id}/frontend/{asset_path}?v={version}"


def resolve_frontend_asset(extension_id: str, asset_path: str) -> Path:
    record = get_extension(extension_id)
    if not record or not is_extension_active(extension_id):
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
# extension is itself active and runtime-ready.
_QUICK_BUTTON_SUPERSEDED_BY: dict[str, str] = {
    BUILTIN_ASK_EXTENSION_ID: "assistant",
}


def _quick_button_superseded(extension_id: str) -> bool:
    superseder_role = _QUICK_BUTTON_SUPERSEDED_BY.get(extension_id)
    if not superseder_role:
        return False
    superseder = extension_id_for_role(superseder_role)
    if not superseder:
        return False
    return is_extension_active(superseder)


def _project_quick_button_action(
    action: dict[str, Any],
    *,
    extension_id: str,
    frontend_path: str,
) -> dict[str, Any]:
    if action.get("type") != "module":
        return action
    module_url = str(action.get("module_url") or "")
    prefix = f"/api/extensions/{extension_id}/frontend/"
    legacy_prefix = f"/api/extensions/{extension_id}/assets/"
    if module_url.startswith(prefix):
        module_url = module_url[len(prefix):]
    elif module_url.startswith(legacy_prefix):
        module_url = module_url[len(legacy_prefix):]
    return {
        "type": "module",
        "module_url": _extension_frontend_module_url(
            module_url,
            field="entrypoints.quick_button.action.module_url",
            frontend_path=frontend_path,
            extension_id=extension_id,
        ),
    }


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
        frontend_path = str(entrypoints.get("frontend") or "")

        quick_button = entrypoints.get("quick_button") or {}
        if (
            quick_button
            and _ui_hook_enabled(settings, extension_id, "quick_button_enabled")
            and not _quick_button_superseded(extension_id)
        ):
            action = quick_button.get("action") or {}
            try:
                projected_action = _project_quick_button_action(
                    action if isinstance(action, dict) else {},
                    extension_id=extension_id,
                    frontend_path=frontend_path,
                )
            except ExtensionError:
                projected_action = {}
            if not projected_action:
                continue
            item: dict[str, Any] = {
                "extension_id": extension_id,
                "extension_name": extension_name,
                "label": quick_button.get("label", ""),
                # Records installed before placements existed carry none;
                # they surface everywhere, matching the validation default.
                "placements": quick_button.get("placements") or list(QUICK_BUTTON_PLACEMENTS),
                "action": projected_action,
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
    return (
        store_fingerprint(),
        _file_fingerprint(_ui_settings_path()),
        installation_profile.integrations_enabled(),
    )


# ── extension settings + per-MCP-server enable/disable ───────────────
#
# User-configurable, manifest-declared settings (entrypoints.settings) plus a
# per-MCP-server enable/disable toggle. Non-secret values live in
# extension-settings.json; secret-typed values live ONLY in the OS keychain
# (via password_manager) and are never persisted to disk or returned by GET.

_EXT_SETTINGS_SCHEMA_VERSION = 3
_SETTING_SECRET_SERVICE = "better-agent-extension-setting"

# Free-text, user-authored "how to use this extension" instructions. Distinct
# from the author-shipped manifest instruction sections: this is the user's own
# preference text, injected into agent runs only while the extension is active.
_USER_INSTRUCTIONS_MAX_CHARS = 4_000


class ExtensionSettingsSchemaError(ExtensionError):
    def __init__(self, found: Any, revision: str) -> None:
        self.found = found if isinstance(found, int) and not isinstance(found, bool) else None
        self.expected = _EXT_SETTINGS_SCHEMA_VERSION
        self.revision = revision
        super().__init__(
            "Extension settings are incompatible with this Better Agent version"
        )


def _ext_settings_path() -> Path:
    return ba_home() / "extensions" / "extension-settings.json"


def extension_settings_fingerprint() -> tuple[int, int]:
    return _file_fingerprint(_ext_settings_path())


def _blank_ext_settings() -> dict[str, Any]:
    return {"schema_version": _EXT_SETTINGS_SCHEMA_VERSION, "extensions": {}}


def _extension_settings_revision() -> str:
    try:
        content = _ext_settings_path().read_bytes()
    except FileNotFoundError:
        content = b""
    return hashlib.sha256(content).hexdigest()


def _migrate_ext_settings(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("schema_version") not in (1, 2):
        raise ExtensionSettingsSchemaError(
            data.get("schema_version"), _extension_settings_revision()
        )
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        raise ExtensionError("Malformed extension-settings: extensions must be an object")
    migrated = copy.deepcopy(data)
    migrated["schema_version"] = _EXT_SETTINGS_SCHEMA_VERSION
    for extension_id in list(extensions):
        entry = _ext_settings_entry(migrated, extension_id)
        defaults = _DEFAULT_NATIVE_HARNESS_BY_EXTENSION_ID.get(extension_id, ())
        if defaults:
            entry["native_harness"] = sorted(set(entry["native_harness"]).union(defaults))
    _save_ext_settings(migrated)
    _clear_projection_cache()
    return migrated


def _quarantine_ext_settings_path(settings_path: Path, revision: str) -> Path:
    base = settings_path.with_name(
        f"{settings_path.stem}.incompatible-{revision}{settings_path.suffix}"
    )
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = settings_path.with_name(
            f"{settings_path.stem}.incompatible-{revision}.{index}{settings_path.suffix}"
        )
        if not candidate.exists():
            return candidate
    raise ExtensionError("Extension settings could not be quarantined")


def _load_ext_settings() -> dict[str, Any]:
    with _EXT_SETTINGS_LOCK:
        data = read_json(_ext_settings_path(), _blank_ext_settings())
        if data.get("schema_version") != _EXT_SETTINGS_SCHEMA_VERSION:
            return _migrate_ext_settings(data)
        extensions = data.get("extensions")
        if not isinstance(extensions, dict):
            raise ExtensionError("Malformed extension-settings: extensions must be an object")
        return data


def _save_ext_settings(data: dict[str, Any]) -> None:
    with _EXT_SETTINGS_LOCK:
        write_json(_ext_settings_path(), data)
    _clear_projection_cache()


def _invalidate_harness_profile_resolver_cache() -> None:
    try:
        import harness_profile_resolver

        harness_profile_resolver.invalidate_cache()
    except Exception:
        logger.debug("failed to invalidate harness profile resolver cache", exc_info=True)


def _set_native_harness_value(
    extension_id: str,
    key: str,
    enabled: bool,
) -> tuple[list[str], list[str]]:
    with _EXT_SETTINGS_LOCK:
        data = _load_ext_settings()
        entry = _ext_settings_entry(data, extension_id)
        previous = list(entry["native_harness"])
        exposed = set(previous)
        if enabled:
            exposed.add(key)
        else:
            exposed.discard(key)
        attempted = sorted(exposed)
        entry["native_harness"] = attempted
        _save_ext_settings(data)
        return previous, list(attempted)


def _restore_native_harness_if_unchanged(
    extension_id: str,
    *,
    attempted: list[str],
    previous: list[str],
) -> bool:
    with _EXT_SETTINGS_LOCK:
        data = _load_ext_settings()
        entry = _ext_settings_entry(data, extension_id)
        if entry["native_harness"] != attempted:
            return False
        entry["native_harness"] = list(previous)
        _save_ext_settings(data)
        return True


def reset_extension_settings(*, expected_found_schema: int | None, expected_revision: str) -> dict[str, int]:
    with _EXT_SETTINGS_LOCK:
        data = read_json(_ext_settings_path(), _blank_ext_settings())
        current_schema = data.get("schema_version")
        current_found = current_schema if isinstance(current_schema, int) and not isinstance(current_schema, bool) else None
        if current_schema == _EXT_SETTINGS_SCHEMA_VERSION:
            raise ExtensionError("Extension settings are already compatible")
        if current_found != expected_found_schema or _extension_settings_revision() != expected_revision:
            raise ExtensionError("Extension settings changed; reload before resetting")
        settings_path = _ext_settings_path()
        if settings_path.exists():
            settings_path.replace(_quarantine_ext_settings_path(settings_path, expected_revision))
    _clear_projection_cache()
    return {"schema_version": _EXT_SETTINGS_SCHEMA_VERSION}


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
    if "native_harness" not in entry:
        entry["native_harness"] = list(_DEFAULT_NATIVE_HARNESS_BY_EXTENSION_ID.get(extension_id, ()))
    if not isinstance(entry["native_harness"], list) or not all(
        isinstance(item, str) for item in entry["native_harness"]
    ):
        raise ExtensionError("Malformed extension-settings: native_harness must be a string list")
    for key in entry["native_harness"]:
        kind, separator, name = key.partition(":")
        if separator != ":" or kind not in _NATIVE_HARNESS_KINDS or not _ID_RE.fullmatch(name):
            raise ExtensionError("Malformed extension-settings: invalid native_harness key")
    return entry


def _setting_schema_list(extension_id: str) -> list[dict[str, Any]]:
    record = get_extension(extension_id)
    if not record:
        return []
    return list(record["manifest"].get("entrypoints", {}).get("settings") or [])


def _setting_secret_account(extension_id: str, key: str) -> str:
    # ":" rather than "/": password_manager rejects "/" in an account, so a
    # slash-joined account made every read of a secret-typed setting raise.
    return f"{extension_id}:{key}"


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
        _invalidate_harness_profile_resolver_cache()
        return get_extension_settings(extension_id)
    coerced = _coerce_setting_value(value, spec["type"], key, enum=spec.get("enum"))
    data = _load_ext_settings()
    _ext_settings_entry(data, extension_id)["values"][key] = coerced
    _save_ext_settings(data)
    return get_extension_settings(extension_id)


def resolve_all_settings(
    extension_id: str,
    *,
    inputs: dict[str, Any] | None = None,
    include_secrets: bool = True,
) -> dict[str, Any]:
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
            if include_secrets:
                try:
                    resolved[key] = password_manager.get_service_password(
                        _SETTING_SECRET_SERVICE, _setting_secret_account(extension_id, key)
                    )
                except Exception:
                    resolved[key] = ""
            else:
                resolved[key] = ""
        else:
            resolved[key] = stored_values.get(key, item.get("default"))
    if inputs:
        overlays = _profile_setting_overlays(inputs, extension_id)
        for item in schema:
            key = item["key"]
            if item["type"] != "secret" and key in overlays:
                resolved[key] = copy.deepcopy(overlays[key])
    return resolved


def mcp_forcing_skills(
    extension_id: str,
    server_name: str,
    *,
    settings: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> list[str]:
    """Names of enabled skills whose requires_mcp covers this server — these
    force it on so the skill's instructions stay executable. requires_mcp is
    True (all of the extension's servers) or a list of server names."""
    current_record = record if record is not None else get_extension(extension_id)
    if current_record is None:
        return []
    entrypoints = (current_record.get("manifest") or {}).get("entrypoints") or {}
    data = settings if settings is not None else _load_ext_settings()
    forcing: list[str] = []
    for item in entrypoints.get("skills") or []:
        if not isinstance(item, dict):
            continue
        requires = item.get("requires_mcp")
        covers = requires is True or (isinstance(requires, list) and server_name in requires)
        if covers and is_runtime_skill_enabled(
            extension_id, item["name"], settings=data, record=current_record
        ):
            forcing.append(item["name"])
    return forcing


def is_mcp_server_enabled(
    extension_id: str,
    server_name: str,
    *,
    settings: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> bool:
    """Explicit user choice wins (mcp_enabled map, legacy mcp_disabled list),
    else the manifest item's default_enabled (default True). A disabled outcome
    is overridden while an enabled requires_mcp skill forces the server on."""
    data = settings if settings is not None else _load_ext_settings()
    entry = data["extensions"].get(extension_id, {})
    explicit: bool | None = None
    if isinstance(entry, dict):
        overrides = entry.get("mcp_enabled")
        if isinstance(overrides, dict) and server_name in overrides:
            explicit = bool(overrides[server_name])
        else:
            disabled = entry.get("mcp_disabled")
            if isinstance(disabled, list) and server_name in set(disabled):
                explicit = False
    if explicit is None:
        current_record = record if record is not None else get_extension(extension_id)
        item = _harness_addition(current_record, "mcp", server_name) if current_record else None
        explicit = bool(item.get("default_enabled", True)) if item else True
    if explicit:
        return True
    return bool(mcp_forcing_skills(extension_id, server_name, settings=data, record=record))


def is_runtime_skill_enabled(
    extension_id: str,
    skill_name: str,
    *,
    settings: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> bool:
    """Whether a declared skill is exposed to sessions. Explicit user choice
    wins; otherwise the manifest item's default_enabled (default True)."""
    current_record = record if record is not None else get_extension(extension_id)
    if current_record is None:
        return False
    item = _harness_addition(current_record, "skill", skill_name)
    if item is None:
        return False
    data = settings if settings is not None else _load_ext_settings()
    entry = data["extensions"].get(extension_id, {})
    overrides = entry.get("skills_enabled") if isinstance(entry, dict) else None
    if isinstance(overrides, dict) and skill_name in overrides:
        return bool(overrides[skill_name])
    return bool(item.get("default_enabled", True))


def set_runtime_skill_enabled(extension_id: str, skill_name: str, enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ExtensionError("skill enabled must be a boolean")
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    if not _ID_RE.fullmatch(skill_name):
        raise ExtensionError("Invalid skill name")
    if _harness_addition(record, "skill", skill_name) is None:
        raise ExtensionError("Unknown skill")
    data = _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    overrides = entry.get("skills_enabled")
    if not isinstance(overrides, dict):
        overrides = {}
    overrides[skill_name] = enabled
    entry["skills_enabled"] = overrides
    _save_ext_settings(data)
    reconcile_runtime_skills()
    return enabled


def _native_harness_key(kind: str, name: str) -> str:
    clean_kind = str(kind or "").strip()
    clean_name = str(name or "").strip()
    if clean_kind not in _NATIVE_HARNESS_KINDS:
        raise ExtensionError(f"Unknown harness addition kind: {clean_kind}")
    if not _ID_RE.fullmatch(clean_name):
        raise ExtensionError("Invalid harness addition name")
    return f"{clean_kind}:{clean_name}"


def _harness_addition(record: dict[str, Any], kind: str, name: str) -> dict[str, Any] | None:
    entrypoints = (record.get("manifest") or {}).get("entrypoints") or {}
    if kind == "instructions":
        items = extension_instructions.instruction_items_from_entrypoints(entrypoints) or []
    elif kind == "skill":
        items = entrypoints.get("skills") or []
    elif kind == "mcp":
        items = _stored_mcp_entrypoints(record)
    else:
        return None
    return next(
        (item for item in items if isinstance(item, dict) and str(item.get("name") or "") == name),
        None,
    )


def _native_harness_eligible(record: dict[str, Any], kind: str, name: str) -> bool:
    item = _harness_addition(record, kind, name)
    if item is None:
        return False
    if kind != "mcp":
        return True
    return bool(
        item.get("ambient_native") is True
        and item.get("user_facing") is False
        and item.get("requires_backend_auth") is False
        and not item.get("predicate")
    )


def native_harness_exposed(
    extension_id: str,
    kind: str,
    name: str,
    *,
    settings: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> bool:
    key = _native_harness_key(kind, name)
    current_record = record if record is not None else get_extension(extension_id)
    if current_record is None or not _native_harness_eligible(current_record, kind, name):
        return False
    data = settings if settings is not None else _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    return key in set(entry["native_harness"])


def set_native_harness_exposed(extension_id: str, kind: str, name: str, enabled: bool) -> bool:
    if kind == "mcp":
        # Native MCP exposure is managed via grant_native_mcp_server()/
        # revoke_native_mcp_server(). Reject rather than silently
        # accept-and-do-nothing, which would tell a caller
        # {"native_exposed": true} while nothing actually changed.
        raise ExtensionError(
            "native exposure for kind='mcp' is managed via "
            "grant_native_mcp_server()/revoke_native_mcp_server(), not this flag"
        )
    if not isinstance(enabled, bool):
        raise ExtensionError("native exposure enabled must be a boolean")
    key = _native_harness_key(kind, name)
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    if _harness_addition(record, kind, name) is None:
        raise ExtensionError("Unknown harness addition")
    if enabled and not _native_harness_eligible(record, kind, name):
        raise ExtensionError("Harness addition is not safe for ambient native tools")
    previous, attempted = _set_native_harness_value(extension_id, key, enabled)
    try:
        if kind == "skill":
            reconcile_runtime_skills()
    except Exception as exc:
        restored = _restore_native_harness_if_unchanged(
            extension_id,
            attempted=attempted,
            previous=previous,
        )
        try:
            if restored:
                if kind == "skill":
                    reconcile_runtime_skills()
        except Exception:
            pass
        raise ExtensionError(f"Could not apply native exposure: {exc}") from exc
    return enabled


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
    version = _frontend_asset_version(record)
    result: list[dict[str, Any]] = []
    for item in frontend_modules:
        module_path = str(item.get("module") or "")
        enabled = is_frontend_module_enabled(extension_id, item["slot"], item["id"])
        module_url = (
            _frontend_asset_url(manifest["id"], module_path, version)
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
    """Capability-context block carrying extension and user instructions.

    Active, runtime-ready extensions contribute their author-shipped instruction
    sections and any non-empty user instructions. The provider-uniform context
    reaches every runner and is re-read fresh each turn.
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
        blocks.extend(extension_instructions.runtime_instruction_blocks(record))
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
        "Instructions for installed extensions follow. Apply each block when "
        "using the matching extension's tools or features.\n\n"
        + "\n\n".join(blocks)
    )
    return [{
        "name": "Extension Instructions",
        "category": "instructions",
        "content_kind": "extension_user_instructions",
        "content": content,
    }]


def set_mcp_server_enabled(extension_id: str, server_name: str, enabled: bool) -> bool:
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    if not _ID_RE.fullmatch(server_name):
        raise ExtensionError("Invalid MCP server name")
    if not enabled:
        forcing = mcp_forcing_skills(extension_id, server_name, record=record)
        if forcing:
            raise ExtensionError(
                f"MCP server {server_name!r} is required by enabled skill(s): "
                f"{', '.join(forcing)}. Disable the skill first."
            )
    data = _load_ext_settings()
    entry = _ext_settings_entry(data, extension_id)
    overrides = entry.get("mcp_enabled")
    if not isinstance(overrides, dict):
        overrides = {}
    overrides[server_name] = enabled
    entry["mcp_enabled"] = overrides
    legacy_disabled = set(entry.get("mcp_disabled") or [])
    legacy_disabled.discard(server_name)
    entry["mcp_disabled"] = sorted(legacy_disabled)
    _save_ext_settings(data)
    return enabled


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
        "harness_additions": extension_harness_additions(record),
        "internal_llm_tasks": extension_internal_llm_tasks(record),
        "user_instructions": get_user_instructions(extension_id),
        "ui": get_ui_settings(extension_id),
        "frontend_modules": extension_frontend_modules(extension_id),
        "mcp": extension_mcp_servers(extension_id),
        "skills": extension_runtime_skills(extension_id),
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
    return _extension_internal_llm_tasks(
        manifest,
        _EXTENSION_SETTINGS_INTERNAL_LLM_TASKS.get(extension_id, ()),
    )


def extension_provisioned_internal_llm_tasks(record: dict[str, Any]) -> list[str]:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    return _extension_internal_llm_tasks(
        manifest,
        _BUILTIN_INTERNAL_LLM_TASKS.get(extension_id, ()),
    )


def _declared_internal_llm_tasks(manifest: dict[str, Any]) -> list[str]:
    """Task keys an extension declares for itself via
    ``permissions.internal_llm_tasks``. This is the structural source of truth:
    any extension that needs an internal LLM declares its task key here and
    automatically gets an override row in its own settings + a readiness gate,
    defaulting to Inherit. Hardcoded maps below are the legacy fallback for
    builtin extensions only."""
    declared = ((manifest.get("permissions") or {}).get("internal_llm_tasks") or [])
    tasks: list[str] = []
    for task in declared:
        key = str(task).strip()
        if key and key not in tasks:
            tasks.append(key)
    return tasks


def _extension_internal_llm_tasks(
    manifest: dict[str, Any],
    extension_tasks: tuple[str, ...],
) -> list[str]:
    tasks = list(extension_tasks)
    for task in _declared_internal_llm_tasks(manifest):
        if task not in tasks:
            tasks.append(task)
    for role in manifest.get("core_roles") or []:
        for task in _CORE_ROLE_INTERNAL_LLM_TASKS.get(str(role), ()):
            if task not in tasks:
                tasks.append(task)
    return tasks


def all_internal_llm_task_keys() -> list[str]:
    """Every internal-LLM task key contributed by builtin extensions (public
    and private-registry) or declared via ``permissions.internal_llm_tasks``
    on an installed extension, in stable declaration order. Absent private
    checkout ⇒ private tasks are simply not contributed."""
    keys: list[str] = []
    task_groups = [
        *_BUILTIN_INTERNAL_LLM_TASKS.values(),
        *_CORE_ROLE_INTERNAL_LLM_TASKS.values(),
    ]
    for task_keys in task_groups:
        for key in task_keys:
            if key not in keys:
                keys.append(key)
    for record in list_extensions():
        for key in _declared_internal_llm_tasks(record.get("manifest") or {}):
            if key not in keys:
                keys.append(key)
    return keys


def internal_llm_task_labels() -> dict[str, str]:
    return {}


def extension_internal_llm_task_keys() -> set[str]:
    task_keys: set[str] = set()
    for keys in _EXTENSION_SETTINGS_INTERNAL_LLM_TASKS.values():
        task_keys.update(keys)
    for keys in _CORE_ROLE_INTERNAL_LLM_TASKS.values():
        task_keys.update(keys)
    for record in list_extensions():
        task_keys.update(_declared_internal_llm_tasks(record.get("manifest") or {}))
    return task_keys


def extension_harness_additions(record: dict[str, Any]) -> list[dict[str, Any]]:
    """... For kind="mcp" items, `native_exposed` reports GLOBAL grant status
    only -- this function has no project context to resolve project-scope
    grants against, so a server granted at project scope (PR2+) reports as
    not exposed here. Callers must not read this field as "exposed
    anywhere"; it is specifically "exposed globally"."""
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    entrypoints = manifest.get("entrypoints") or {}
    additions: list[dict[str, Any]] = []
    for item in extension_instructions.instruction_items_from_entrypoints(entrypoints) or []:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            additions.append({
                "kind": "instructions",
                "name": name,
                "detail": "project" if item.get("level") == "project" else "global",
                "native_eligible": True,
                "native_exposed": native_harness_exposed(extension_id, "instructions", name, record=record),
            })
    for item in entrypoints.get("skills") or []:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            additions.append({
                "kind": "skill",
                "name": name,
                "detail": "enabled" if is_runtime_skill_enabled(extension_id, name, record=record) else "disabled",
                "native_eligible": True,
                "native_exposed": native_harness_exposed(extension_id, "skill", name, record=record),
            })
    # Reuses resolve_native_mcp_servers_for_context() (no project_path -> only
    # global grants match) rather than re-checking grant/digest here -- one
    # predicate, not a second copy that can drift.
    granted_global = resolve_native_mcp_servers_for_context()
    for item in _stored_mcp_entrypoints(record):
        name = str(item.get("name") or "")
        if not name or name in _RESERVED_MCP_SERVER_NAMES:
            continue
        server_id = _native_mcp_server_id(item)
        additions.append({
            "kind": "mcp",
            "name": name,
            "detail": "enabled" if is_mcp_server_enabled(str(manifest.get("id") or ""), name, record=record) else "disabled",
            "native_eligible": _native_harness_eligible(record, "mcp", name),
            "native_exposed": f"{extension_id}:{server_id}" in granted_global,
        })
    return additions


def all_extension_mcp_server_names() -> set[str]:
    """Server names declared by installed, enabled extensions — the valid
    targets for per-session MCP opt-in."""
    names: set[str] = set()
    for record in _active_records():
        for item in _stored_mcp_entrypoints(record):
            names.add(item["name"])
    return names


def extension_runtime_skills(extension_id: str) -> list[dict[str, Any]]:
    """Skills an extension provides, with current session-exposure state — for
    the Settings UI."""
    record = get_extension(extension_id)
    if record is None:
        raise ExtensionError("Extension not installed")
    entrypoints = record["manifest"].get("entrypoints", {})
    root = runtime_package_root_for_record(record)
    return [
        {
            "name": item["name"],
            "description": extension_descriptions.skill_description(root, item),
            "description_path": extension_descriptions.skill_description_path(root, item),
            "enabled": is_runtime_skill_enabled(extension_id, item["name"], record=record),
        }
        for item in entrypoints.get("skills") or []
        if isinstance(item, dict) and item.get("name")
    ]


def extension_harness_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in _active_records():
        manifest = record.get("manifest") or {}
        extension_id = str(manifest.get("id") or "").strip()
        if not extension_id or not is_extension_runtime_ready(extension_id):
            continue
        root = runtime_package_root_for_record(record)
        if root is None:
            continue
        package_root = root.resolve()
        for item in (manifest.get("entrypoints") or {}).get("harness_profiles") or []:
            profile_id = str((item or {}).get("id") or "").strip()
            if not profile_id or profile_id in seen:
                raise ExtensionError(f"Duplicate harness profile id: {profile_id}")
            path = (package_root / str(item.get("path") or "")).resolve()
            if not path.is_relative_to(package_root) or not path.is_file():
                raise ExtensionError(f"Harness profile file not found: {extension_id}.{profile_id}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ExtensionError(f"Harness profile is invalid JSON: {extension_id}.{profile_id}") from exc
            if not isinstance(payload, dict):
                raise ExtensionError(f"Harness profile payload must be an object: {extension_id}.{profile_id}")
            profile = {
                **payload,
                "id": profile_id,
                "name": str(item.get("name") or payload.get("name") or profile_id).strip(),
                "description": str(item.get("description") or payload.get("description") or "").strip(),
                "source": f"extension:{extension_id}",
                "extension_id": extension_id,
                "read_only": True,
            }
            seen.add(profile_id)
            profiles.append(profile)
    return profiles


def extension_mcp_servers(extension_id: str) -> list[dict[str, Any]]:
    """MCP servers an extension provides, with current enabled state — for the
    Settings UI."""
    if get_extension(extension_id) is None:
        raise ExtensionError("Extension not installed")
    record = get_extension(extension_id)
    root = runtime_package_root_for_record(record)
    servers: list[dict[str, Any]] = []
    for item in _stored_mcp_entrypoints(record):
        if item["name"] in _RESERVED_MCP_SERVER_NAMES:
            continue
        servers.append(
            {
                "name": item["name"],
                "label": item.get("label") or item["name"],
                "description": extension_descriptions.mcp_description(root, item),
                "description_path": extension_descriptions.mcp_description_path(root, item),
                "user_facing": item.get("user_facing", True),
                "enabled": is_mcp_server_enabled(extension_id, item["name"], record=record),
                "forced_by_skills": mcp_forcing_skills(extension_id, item["name"], record=record),
            }
        )
    return servers
