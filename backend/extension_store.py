from __future__ import annotations

import copy
import errno
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
import logging
import sys
import base64
import hashlib
import tarfile
import urllib.error
import urllib.request
import uuid
import atexit
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from env_compat import better_agent_runtime_env, dual_env_many, get_env
from json_store import read_json, write_json
from paths import ba_home
import password_manager
import extension_applied_config
from provider_config_sync_backend.api import KNOWN_PROVIDER_KINDS
import extension_instructions
import extension_mcp
import perf
import extension_integrity

logger = logging.getLogger(__name__)

STORE_SCHEMA_VERSION = 2
MANIFEST_KIND = "better-agent-extension"
EXTENSION_SLOW_CALL_SECONDS = 2.0
_EXTENSION_SLOW_CALL_LIMIT = 3
_EXTENSION_SLOW_CALL_WINDOW_SECONDS = 10 * 60.0
# Ceilings on author-declared manifest durations, so a route legitimately
# allowed to run long can't also silently blind the hang/slow-call
# guardrails for an unbounded time.
MAX_BACKEND_TIMEOUT_SECONDS = 1800.0
MAX_SLOW_CALL_GRACE_SECONDS = 180.0

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]{0,127}$")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_GIT_SCP_RE = re.compile(r"^git@[A-Za-z0-9_.-]+:[A-Za-z0-9_.~/-]+\.git$")
_ALLOWED_SURFACES = {"backend_feature", "frontend_feature", "runtime_mcp", "instructions", "skills", "daemons"}
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
_RUNTIME_READINESS_REFRESH_LOCK = threading.Lock()
_RUNTIME_READINESS_REFRESH_GENERATION = 0
_RUNTIME_STORE_GENERATION = 0
_RUNTIME_INTEGRITY_WORKER: _RuntimeIntegrityWorker | None = None
_RUNTIME_INTEGRITY_EXECUTOR_LOCK = threading.Lock()
_RUNTIME_READINESS_CHANGE = threading.Event()
StoreFingerprint = tuple[str, str]
_ENABLED_CACHE: dict[str, tuple[StoreFingerprint, bool]] = {}
_ENABLED_CACHE_LOCK = threading.Lock()
# Fingerprint-keyed cache for get_extension() — defined here (beside the
# other store caches) so _clear_projection_cache can reference it.
_GET_EXTENSION_CACHE: dict[str, tuple[StoreFingerprint, dict[str, Any] | None]] = {}
_GET_EXTENSION_CACHE_LOCK = threading.Lock()
_BUILTIN_FEATURE_CACHE: dict[str, tuple[StoreFingerprint, bool]] = {}
_BUILTIN_FEATURE_CACHE_LOCK = threading.Lock()
_STORE_FINGERPRINT_CACHE: tuple[float, StoreFingerprint] | None = None
_STORE_FINGERPRINT_CACHE_LOCK = threading.Lock()
_STORE_FINGERPRINT_TTL_SECONDS = 0.5
_RECONCILED_STORE_FINGERPRINT: tuple[str, StoreFingerprint] | None = None
_RECONCILED_STORE_LOCK = threading.Lock()
_CORE_ROLE_OWNERS_CACHE: tuple[StoreFingerprint, MappingProxyType] | None = None
_CORE_ROLE_OWNERS_LOCK = threading.Lock()
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
    "provider-config-sync",
    "better-agent-coordination",
    "session-bridge",
}

CORE_ROLES = frozenset({
    "adv", "agent-board", "assistant", "auto-tagging", "browser-harness", "canvas",
    "credential-broker", "machine-nodes", "project-structure",
    "prompt-engineer", "requirements", "routines", "scheduler",
    "supervisor", "team-orchestration", "testape",
})


# Public builtin ids stay literal in the public repo.
BUILTIN_ASK_EXTENSION_ID = "ofek-dev.ask"
ASSISTANT_EXTENSION_ID = "ofek-dev.assistant"
BUILTIN_SESSION_BRIDGE_EXTENSION_ID = "ofek-dev.session-bridge"
BUILTIN_SESSION_CONTROL_EXTENSION_ID = "ofek-dev.session-control"
BUILTIN_COORDINATION_EXTENSION_ID = "ofek-dev.coordination"
BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID = "ofek-dev.provider-config-sync"
BUILTIN_TODOS_EXTENSION_ID = "ofek-dev.todos"
BUILTIN_HARNESS_INSTRUCTIONS_EXTENSION_ID = "better-agent.harness-for-better-agent"
BUILTIN_USER_ATTENTION_EXTENSION_ID = "ofek-dev.user-attention"
BUILTIN_SWITCH_CONTROL_EXTENSION_ID = "ofek-dev.switch-control"
_BUILTIN_MCP_REPLACEMENTS_BY_EXTENSION_ID = {
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: frozenset({"provider-config-sync"}),
    BUILTIN_COORDINATION_EXTENSION_ID: frozenset({"better-agent-coordination"}),
}
_MCP_REPLACEMENT_CORE_ROLES = {
    "project-updates": "project-structure",
    "get-requirements": "requirements",
    "credential-broker": "credential-broker",
}
MARKETPLACE_EXTENSION_ID = "ofek-dev.marketplace"
REQUIRED_EXTENSION_IDS = {MARKETPLACE_EXTENSION_ID}
PUBLIC_EXTENSION_LIST_HIDDEN_IDS = frozenset()
_OBSOLETE_EXTENSION_IDS = {
    "better-agent.marketplace": MARKETPLACE_EXTENSION_ID,
    "ofek-dev.needs-user-decision": BUILTIN_USER_ATTENTION_EXTENSION_ID,
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
    BUILTIN_SWITCH_CONTROL_EXTENSION_ID: "extensions/switch-control",
    MARKETPLACE_EXTENSION_ID: "extensions/marketplace",
}
_EXTENSION_DISPLAY_NAMES = {
    BUILTIN_ASK_EXTENSION_ID: "Ask",
    BUILTIN_SESSION_BRIDGE_EXTENSION_ID: "Session Bridge",
    BUILTIN_SESSION_CONTROL_EXTENSION_ID: "Session Control",
    BUILTIN_COORDINATION_EXTENSION_ID: "Coordination",
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: "Provider Config Sync",
    BUILTIN_TODOS_EXTENSION_ID: "Todos",
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
_required_artifact_update_checked: set[str] = set()

_BUILTIN_INTERNAL_LLM_TASKS: dict[str, tuple[str, ...]] = {
    BUILTIN_ASK_EXTENSION_ID: ("session_search_worker",),
    BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: ("provider_config_sync_review",),
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
        {BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID: ("provider_config_sync_review",)}
        if BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID
        else {}
    ),
    **(
        {BUILTIN_SESSION_BRIDGE_EXTENSION_ID: ("delegation_session_bridge",)}
        if BUILTIN_SESSION_BRIDGE_EXTENSION_ID
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
    "providerConfigSync": BUILTIN_PROVIDER_CONFIG_SYNC_EXTENSION_ID,
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


_STORE_PATH: Path | None = None
_STORE_PATH_HOME_KEY: str | None = None


def _store_path() -> Path:
    global _STORE_PATH, _STORE_PATH_HOME_KEY
    home = ba_home()
    home_key = str(home)
    if _STORE_PATH is None or _STORE_PATH_HOME_KEY != home_key:
        _STORE_PATH = home / "extensions" / "extensions.json"
        _STORE_PATH_HOME_KEY = home_key
    return _STORE_PATH


# Test-only synchronous overrides keep the path and its home identity paired.
@contextmanager
def _override_store_path(path: Path):
    global _STORE_PATH, _STORE_PATH_HOME_KEY
    previous = (_STORE_PATH, _STORE_PATH_HOME_KEY)
    _STORE_PATH = path
    _STORE_PATH_HOME_KEY = str(ba_home())
    try:
        yield
    finally:
        _STORE_PATH, _STORE_PATH_HOME_KEY = previous


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


def store_fingerprint() -> StoreFingerprint:
    global _STORE_FINGERPRINT_CACHE
    now = time.monotonic()
    with _STORE_FINGERPRINT_CACHE_LOCK:
        cached = _STORE_FINGERPRINT_CACHE
        current_path = str(_store_path())
        if (
            cached is not None
            and now - cached[0] <= _STORE_FINGERPRINT_TTL_SECONDS
            and cached[1][0] == current_path
        ):
            return cached[1]
    path = _store_path()
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        digest = ""
    fingerprint = (str(path), digest)
    with _STORE_FINGERPRINT_CACHE_LOCK:
        _STORE_FINGERPRINT_CACHE = (now, fingerprint)
    return fingerprint


def _refresh_store_fingerprint_cache(path: Path | None = None) -> StoreFingerprint:
    global _STORE_FINGERPRINT_CACHE
    path = path or _store_path()
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        digest = ""
    fingerprint = (str(path), digest)
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
    global _RECONCILED_STORE_FINGERPRINT, _CORE_ROLE_OWNERS_CACHE, _RUNTIME_STORE_GENERATION
    _PROJECTION_CACHE.clear()
    with _RUNTIME_READY_PROJECTION_LOCK:
        _RUNTIME_STORE_GENERATION += 1
        _RUNTIME_READY_PROJECTION.clear()
        _RUNTIME_PACKAGE_FINGERPRINTS.clear()
    _RUNTIME_READINESS_CHANGE.set()
    with _RECONCILED_STORE_LOCK:
        _RECONCILED_STORE_FINGERPRINT = None
    # get_extension's fingerprint cache auto-invalidates on any store write
    # (file mtime/size changes), but a same-fingerprint forced refresh must
    # drop it too so a reconcile that rewrites identical bytes is observed.
    with _GET_EXTENSION_CACHE_LOCK:
        _GET_EXTENSION_CACHE.clear()
    with _CORE_ROLE_OWNERS_LOCK:
        _CORE_ROLE_OWNERS_CACHE = None


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
    _validate_store_record_identities(extensions)
    if not isinstance(data.get("deleted_extensions"), dict):
        data["deleted_extensions"] = {}
    if _annotate_legacy_quarantine_cohorts(data):
        _write_store_unlocked(data)
    return data


def _validate_store_record_identities(extensions: dict[Any, Any]) -> None:
    for extension_id, record in extensions.items():
        if not isinstance(extension_id, str) or not _ID_RE.fullmatch(extension_id):
            raise ExtensionError("Malformed extension store: extension id is invalid")
        if not isinstance(record, dict):
            raise ExtensionError(
                f"Malformed extension store: record for {extension_id!r} must be an object"
            )
        manifest = record.get("manifest")
        if not isinstance(manifest, dict) or manifest.get("id") != extension_id:
            raise ExtensionError(
                f"Malformed extension store: manifest id for {extension_id!r} must match its record key"
            )


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
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        raise ExtensionError("Malformed extension store: extensions must be an object")
    _validate_store_record_identities(extensions)
    previous_destination = _assistant_destination_identity_from_path(_store_path())
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
    current_destination = _assistant_destination_identity(data)
    if current_destination != previous_destination:
        try:
            import lag_incident_queue

            lag_incident_queue.synchronize_destination(
                _assistant_destination_identity_token(current_destination)
            )
        except Exception:
            logger.exception("assistant lag-report destination change notification failed")


def _assistant_destination_identity(data: dict[str, Any]) -> tuple[str, bool]:
    record = (data.get("extensions") or {}).get(ASSISTANT_EXTENSION_ID)
    if not isinstance(record, dict):
        return "", False
    manifest = record.get("manifest") or {}
    entrypoints = manifest.get("entrypoints") or {}
    has_surface = bool(entrypoints.get("backend") or entrypoints.get("backend_module"))
    available = bool(
        has_surface
        and has_permission(record, "backend_routes")
        and _record_active(record)
        and _record_backend_surface_ready(record)
    )
    return _record_generation(record), available


def _assistant_destination_identity_token(identity: tuple[str, bool]) -> str:
    generation, available = identity
    return hashlib.sha256(
        json.dumps([generation, available], separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def synchronize_assistant_destination() -> bool:
    """Repair a destination notification missed between store replace and wake."""
    import lag_incident_queue

    return lag_incident_queue.synchronize_destination(
        _assistant_destination_identity_token(
            _assistant_destination_identity_from_path(_store_path())
        )
    )


def _assistant_destination_identity_from_path(path: Path) -> tuple[str, bool]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return "", False
    if not isinstance(data, dict):
        return "", False
    return _assistant_destination_identity(data)


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


def _unreconciled_run_paths() -> set[Path] | None:
    from active_run_catalog import load_or_rebuild
    from runs_dir import runs_root

    root = runs_root()
    catalog, _rebuilt = load_or_rebuild(root)
    referenced: set[Path] = set()
    if catalog:
        referenced.add(_install_root().resolve())

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
            return
        if isinstance(value, list):
            for nested in value:
                collect(nested)
            return
        if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
            return
        try:
            referenced.add(Path(value).expanduser().resolve())
        except OSError:
            pass

    for run_id in catalog:
        try:
            state = json.loads((root / run_id / "backend_state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict):
            return None
        collect(state)
    return referenced


def _prune_extension_versions(data: dict[str, Any]) -> int:
    """Delete stale on-disk version snapshots for every installed extension.

    Pure disk GC — does not mutate store state. The active install_path is
    always retained; among the remaining version dirs the N newest by mtime
    are kept, the rest removed. Fails open per-dir so one broken entry never
    blocks reconcile. Never deletes outside the extension's versions/ dir.
    """
    root = _install_root().resolve()
    referenced = _unreconciled_run_paths()
    if referenced is None:
        perf.record_count("extension_version_gc.unreadable_run_abort", 1)
        return 0
    if root in referenced:
        perf.record_count("extension_version_gc.unreconciled_run_abort", 1)
        return 0
    removed = 0
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
            if (
                resolved == active
                or not resolved.is_relative_to(versions_resolved)
                or any(path == resolved or path.is_relative_to(resolved) for path in referenced)
            ):
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
            try:
                resolved = stale.resolve(strict=True)
            except OSError:
                continue
            if stale.is_symlink() or not resolved.is_relative_to(versions_resolved):
                continue
            if resolved == active or any(path == resolved or path.is_relative_to(resolved) for path in referenced):
                continue
            shutil.rmtree(stale, ignore_errors=True)
            if not stale.exists():
                removed += 1
    return removed


def prune_extension_versions() -> int:
    with _store_lock():
        data = _read_store_unlocked()
    return _prune_extension_versions(data)


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
    return changed, public_changed, recovered


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
            unknown = sorted(set(providers) - KNOWN_PROVIDER_KINDS)
            if unknown:
                raise ExtensionError(
                    f"entrypoints.instructions.providers has unknown provider kinds: {', '.join(unknown)} "
                    f"(known: {sorted(KNOWN_PROVIDER_KINDS)})"
                )
            normalized["providers"] = sorted(set(providers))
        items.append(normalized)
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
_TAG_RULE_CLEAR_ON = {"view", "new_turn"}


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
        if "ambient_native" in item:
            raise ExtensionError("entrypoints.mcp.ambient_native is replaced by native_exposure")
        native_exposure_raw = item.get("native_exposure") or {}
        if not isinstance(native_exposure_raw, dict):
            raise ExtensionError("entrypoints.mcp.native_exposure must be an object")
        unknown_native = sorted(set(native_exposure_raw) - {"allowed", "permissions"})
        if unknown_native:
            raise ExtensionError(
                "entrypoints.mcp.native_exposure contains unknown keys: "
                + ", ".join(unknown_native)
            )
        native_allowed = native_exposure_raw.get("allowed", False)
        if not isinstance(native_allowed, bool):
            raise ExtensionError("entrypoints.mcp.native_exposure.allowed must be a boolean")
        native_permissions = native_exposure_raw.get("permissions") or []
        if not isinstance(native_permissions, list) or not all(
            isinstance(permission, str) and permission.strip()
            for permission in native_permissions
        ):
            raise ExtensionError(
                "entrypoints.mcp.native_exposure.permissions must be a string list"
            )
        native_permissions = list(dict.fromkeys(permission.strip() for permission in native_permissions))
        user_facing = item.get("user_facing") is not False
        requires_backend_auth = item.get("requires_backend_auth") is not False
        predicate = _validate_mcp_predicate(item.get("predicate"))
        if native_allowed and requires_backend_auth and not native_permissions:
            raise ExtensionError(
                "authenticated native exposure requires explicit scoped permissions"
            )
        if native_allowed and not requires_backend_auth and native_permissions:
            raise ExtensionError(
                "backend-independent native exposure cannot request backend permissions"
            )
        items.append(
            {
                "name": name,
                "python": python_path,
                "module": module,
                "command": command,
                "args": args,
                "env": env,
                "user_facing": user_facing,
                "bare_allowed": item.get("bare_allowed") is True,
                "requires_backend_auth": requires_backend_auth,
                "native_exposure": {
                    "allowed": native_allowed,
                    "permissions": native_permissions,
                },
                "replaces_builtin": replaces_builtin,
                "predicate": predicate,
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
        "marketplace_auth",
        "reads_session_fields",
        "mutates_session_fields",
        "managed_run_env",
        "capabilities",
        "in_process_execution",
        "daemons",
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
        unknown = sorted(set(item) - {"name", "module", "lifecycle", "restart_policy", "env_allowlist", "ports"})
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


_ALLOWED_HOOK_KEYS = ("pre_turn", "post_turn", "session_event", "pre_send_advisory")


def _validate_hooks(value: Any, *, has_backend: bool) -> dict[str, Any]:
    """Declarative lifecycle hooks an extension subscribes to. Today:
    ``pre_turn`` — core invokes fire-and-forget before a turn runs (on
    ``lifecycle.turn_start``); ``post_turn`` — core invokes fire-and-forget
    after ``lifecycle.turn_complete``; ``session_event`` — per session event;
    ``pre_send_advisory`` — core queries synchronously before a prompt is
    sent and surfaces returned advisories to the user. Every hook is a
    backend invocation and requires ``entrypoints.backend``."""
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
    are positive numbers up to ``MAX_BACKEND_TIMEOUT_SECONDS``. Fail closed: a
    malformed entry rejects the whole manifest rather than silently dropping
    to the 30s host default."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ExtensionError("entrypoints.backend_timeouts must be an object")
    result: dict[str, float] = {}
    for key, value in raw.items():
        route = "default" if key == "default" else str(key).strip().strip("/")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExtensionError(f"entrypoints.backend_timeouts['{key}'] must be a number")
        if value <= 0 or value > MAX_BACKEND_TIMEOUT_SECONDS:
            raise ExtensionError(
                f"entrypoints.backend_timeouts['{key}'] must be a positive number "
                f"<= {MAX_BACKEND_TIMEOUT_SECONDS}"
            )
        result[route] = float(value)
    return result


def _validate_path_pattern(key: str, *, field: str) -> str:
    """Normalize/validate a route pattern: ``/``-separated segments, each
    either a literal or a single-segment ``*`` wildcard (matches exactly one
    dynamic path segment, e.g. a resource id). No ``..``/empty segments."""
    pattern = str(key).strip().strip("/")
    if not pattern:
        raise ExtensionError(f"{field}['{key}'] must not be empty")
    for segment in pattern.split("/"):
        if not segment or segment == "..":
            raise ExtensionError(f"{field}['{key}'] has an invalid path segment")
    return pattern


def _validate_slow_call_grace(raw: Any) -> dict[str, float]:
    """Per-route grace period (seconds) exempting a route from the default
    slow-backend-call quarantine SLA (``EXTENSION_SLOW_CALL_SECONDS``). Keys
    are route patterns (exact subpaths, or one ``*`` wildcard per dynamic
    segment, e.g. ``routines/*/run``) or the special ``default``. This is a
    distinct field from ``backend_timeouts`` on purpose: the timeout field
    bounds how long a call may run before it's aborted, this field only
    widens how long a call may take before it starts counting as a
    quarantine strike. Values are positive numbers up to
    ``MAX_SLOW_CALL_GRACE_SECONDS`` — capped independently so a route
    declaring a long grace period can't also blind hang detection for an
    unbounded time."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ExtensionError("entrypoints.slow_call_grace_seconds must be an object")
    result: dict[str, float] = {}
    for key, value in raw.items():
        pattern = "default" if key == "default" else _validate_path_pattern(
            key, field="entrypoints.slow_call_grace_seconds"
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExtensionError(f"entrypoints.slow_call_grace_seconds['{key}'] must be a number")
        if value <= 0 or value > MAX_SLOW_CALL_GRACE_SECONDS:
            raise ExtensionError(
                f"entrypoints.slow_call_grace_seconds['{key}'] must be a positive number "
                f"<= {MAX_SLOW_CALL_GRACE_SECONDS}"
            )
        result[pattern] = float(value)
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
        "slow_call_grace_seconds": _validate_slow_call_grace(
            entrypoints_raw.get("slow_call_grace_seconds")
        ),
        "backend_retry_on_exit": _validate_backend_retry_on_exit(
            entrypoints_raw.get("backend_retry_on_exit")
        ),
        "daemons": _validate_daemons(entrypoints_raw.get("daemons"), extension_id=extension_id),
    }
    if entrypoints["frontend"] and len(Path(entrypoints["frontend"]).parts) < 2:
        raise ExtensionError("entrypoints.frontend must live under a dedicated asset directory")
    permissions = _validate_permissions(raw.get("permissions"))
    declared_native_permissions = {
        "internal_loopback"
        for value in (permissions.get("internal_loopback"),)
        if value is True
    }
    declared_native_permissions.update(permissions.get("capabilities") or [])
    for mcp_item in entrypoints["mcp"]:
        native_policy = mcp_item.get("native_exposure") or {}
        requested = set(native_policy.get("permissions") or [])
        undeclared = sorted(requested - declared_native_permissions)
        if undeclared:
            raise ExtensionError(
                "entrypoints.mcp.native_exposure requests undeclared permissions: "
                + ", ".join(undeclared)
            )
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


def _rmtree_for_replace(path: Path) -> None:
    """Remove a directory tree before extracting a fresh snapshot.

    A concurrent install/GC pass or a dev file-watcher re-creating entries
    mid-tree-walk surfaces as ENOTEMPTY (Errno 66 on macOS, 39 on Linux) when
    the final ``os.rmdir`` runs. Retry a few times so a transient race does not
    abort an otherwise-fine replace; if it still races, sweep best-effort and
    let the subsequent tar extraction overwrite whatever remains.
    """
    for _ in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise
    shutil.rmtree(path, ignore_errors=True)


def _install_package_artifact(package_dir: Path, target: Path) -> None:
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            _rmtree_for_replace(target)
    except FileNotFoundError:
        pass
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
                extension_instructions.reconcile_blocks(record)
                extension_applied_config.reconcile(record)
                reconcile_runtime_skills()
                reconcile_native_mcp_servers()
        except Exception:
            _save(previous_data)
            for recovered_id in recovered:
                _evict_extension_backend(recovered_id)
            raise
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
    roots: list[Path] = []
    configured = _required_marketplace_repo_root()
    if configured is not None:
        roots.append(configured)
    elif os.environ.get("BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE") != "1":
        roots.append(_repo_root())
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
    _install_package_artifact(package_dir, target)
    try:
        smoke_test = _run_extension_smoke_test(manifest, target)
    except Exception:
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
        extension_instructions.reconcile_blocks(record)
        extension_applied_config.reconcile(record)
    if recovered:
        reconcile_runtime_skills()
        reconcile_native_mcp_servers()


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
        except (ExtensionError, OSError, json.JSONDecodeError):
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
    # Resolve bundled public extensions from the configured catalog checkout
    # or this public repository.
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
    return record.get("enabled") is True and _entitlement_active(record.get("entitlement") or {})


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


def _record_runtime_ready(record: dict[str, Any]) -> bool:
    return _record_runtime_ready_verified(record)


def _record_runtime_ready_projected(record: dict[str, Any]) -> bool:
    manifest = record.get("manifest") or {}
    extension_id = str(manifest.get("id") or "")
    with _RUNTIME_READY_PROJECTION_LOCK:
        return _RUNTIME_READY_PROJECTION.get(extension_id, False)


def refresh_runtime_readiness_projection() -> dict[str, bool]:
    global _RUNTIME_READINESS_REFRESH_GENERATION
    observed_generation = _RUNTIME_READINESS_REFRESH_GENERATION
    wait_started = time.perf_counter()
    with _RUNTIME_READINESS_REFRESH_LOCK:
        perf.record("extension.integrity.singleflight_wait", (time.perf_counter() - wait_started) * 1000.0)
        if _RUNTIME_READINESS_REFRESH_GENERATION != observed_generation:
            perf.record_count("extension.integrity.singleflight_shared", 1)
            with _RUNTIME_READY_PROJECTION_LOCK:
                return dict(_RUNTIME_READY_PROJECTION)
        started = time.perf_counter()
        for attempt in range(3):
            phase_started = time.perf_counter()
            source_fingerprint = _refresh_store_fingerprint_cache()
            perf.record(
                "extension.integrity.store_fingerprint_pre",
                (time.perf_counter() - phase_started) * 1000.0,
            )
            with _RUNTIME_READY_PROJECTION_LOCK:
                source_generation = _RUNTIME_STORE_GENERATION
            phase_started = time.perf_counter()
            records = list_extensions()
            perf.record("extension.integrity.list", (time.perf_counter() - phase_started) * 1000.0)
            phase_started = time.perf_counter()
            result = _build_runtime_readiness_projection(records)
            perf.record("extension.integrity.build", (time.perf_counter() - phase_started) * 1000.0)
            phase_started = time.perf_counter()
            current_fingerprint = _refresh_store_fingerprint_cache()
            perf.record(
                "extension.integrity.store_fingerprint_post",
                (time.perf_counter() - phase_started) * 1000.0,
            )
            phase_started = time.perf_counter()
            with _RUNTIME_READY_PROJECTION_LOCK:
                if (
                    source_generation != _RUNTIME_STORE_GENERATION
                    or source_fingerprint != current_fingerprint
                ):
                    perf.record_count("extension.integrity.cas_retry", 1)
                    continue
                refreshed, fingerprints = result
                _RUNTIME_READY_PROJECTION.clear()
                _RUNTIME_READY_PROJECTION.update(refreshed)
                for extension_id, fingerprint in fingerprints.items():
                    _RUNTIME_PACKAGE_FINGERPRINTS.setdefault(extension_id, fingerprint)
                _RUNTIME_READINESS_REFRESH_GENERATION += 1
                perf.record("extension.integrity.publish", (time.perf_counter() - phase_started) * 1000.0)
                perf.record("extension.integrity.refresh", (time.perf_counter() - started) * 1000.0)
                return dict(refreshed)
        perf.record_count("extension.integrity.cas_failed", 1)
        with _RUNTIME_READY_PROJECTION_LOCK:
            _RUNTIME_READY_PROJECTION.clear()
            _RUNTIME_READINESS_REFRESH_GENERATION += 1
            return {}


def wait_for_runtime_readiness_change(timeout: float) -> bool:
    changed = _RUNTIME_READINESS_CHANGE.wait(timeout)
    if changed:
        _RUNTIME_READINESS_CHANGE.clear()
    return changed


def _build_runtime_readiness_projection(
    records: list[dict[str, Any]],
) -> tuple[dict[str, bool], dict[str, str]]:
    perf.record_count("extension.integrity.extensions", len(records))
    refreshed: dict[str, bool] = {}
    fingerprints: dict[str, str] = {}
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for record in records:
        extension_id = str((record.get("manifest") or {}).get("id") or "")
        ready = _record_active(record) and _record_runtime_ready_verified(record)
        refreshed[extension_id] = ready
        if ready:
            candidates.append((extension_id, record, _runtime_package_integrity_spec(
                record.get("manifest") or {},
                str((record.get("source") or {}).get("install_path") or ""),
            )))
    audited = _runtime_package_fingerprints([spec for _, _, spec in candidates])
    for (extension_id, record, _spec), fingerprint in zip(candidates, audited, strict=True):
        with _RUNTIME_READY_PROJECTION_LOCK:
            previous = (
                str((record.get("smoke_test") or {}).get("runtime_package_sha256") or "")
                or _RUNTIME_PACKAGE_FINGERPRINTS.get(extension_id)
            )
        if fingerprint is None:
            refreshed[extension_id] = False
            perf.record_count("extension.integrity.unverified", 1)
        elif previous is not None and fingerprint != previous:
            refreshed[extension_id] = False
            perf.record_count("extension.integrity.mismatch", 1)
        if fingerprint is not None:
            fingerprints[extension_id] = fingerprint
    return refreshed, fingerprints


def _runtime_package_fingerprint(record: dict[str, Any]) -> str | None:
    spec = _runtime_package_integrity_spec(
        record.get("manifest") or {},
        str((record.get("source") or {}).get("install_path") or ""),
    )
    return _runtime_package_fingerprints([spec])[0]


def _runtime_package_fingerprints(specs: list[dict[str, Any]]) -> list[str | None]:
    if not specs:
        return []
    empty_indexes = {
        index for index, spec in enumerate(specs)
        if not spec["relative_paths"] and not spec["modules"]
    }
    active_specs = [spec for index, spec in enumerate(specs) if index not in empty_indexes]
    if not active_specs:
        return ["" for _ in specs]
    try:
        results = _runtime_integrity_worker().run(active_specs, timeout=2.0)
    except TimeoutError:
        perf.record_count("extension.integrity.timeout", 1)
        logger.error("extension runtime integrity worker exceeded 2s deadline")
        _reset_runtime_integrity_executor(force=True)
        return [None for _ in specs]
    except Exception:
        logger.exception("extension runtime integrity worker failed")
        _reset_runtime_integrity_executor()
        return [None for _ in specs]
    fingerprints: list[str | None] = []
    result_iterator = iter(results)
    for index in range(len(specs)):
        if index in empty_indexes:
            fingerprints.append("")
            continue
        result = next(result_iterator)
        perf.record("extension.integrity.scan", float(result.get("scan_ms") or 0.0))
        perf.record("extension.integrity.hash", float(result.get("hash_ms") or 0.0))
        perf.record_count("extension.integrity.files", int(result.get("files") or 0))
        perf.record_count("extension.integrity.bytes", int(result.get("bytes") or 0))
        fingerprints.append(result.get("digest"))
    return fingerprints


def _runtime_package_integrity_spec(manifest: dict[str, Any], root: str) -> dict[str, Any]:
    protocol = _validate_protocol(manifest.get("protocol"))
    entrypoints = manifest.get("entrypoints") or {}
    relative_paths = set(protocol["smoke_test"].get("required_paths") or [])
    backend_path = str(entrypoints.get("backend") or "")
    if backend_path:
        relative_paths.add(backend_path)
    static_modules = _smoke_static_modules(entrypoints)
    modules = set(protocol["smoke_test"].get("python_modules") or [])
    modules.update(_required_smoke_python_modules(entrypoints))
    raw_root = Path(root).expanduser()
    managed_root = _install_root()
    try:
        raw_root.relative_to(managed_root)
        trusted_root = managed_root
    except ValueError:
        trusted_root = raw_root.parent
    return {
        "root": root,
        "trusted_root": str(trusted_root),
        "relative_paths": sorted(relative_paths),
        "static_modules": static_modules,
        "modules": sorted(modules),
    }


class _RuntimeIntegrityWorker:
    def __init__(self, target: Any = extension_integrity.worker_main) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(
            target=target,
            args=(child,),
            name="extension-integrity-worker",
            daemon=True,
        )
        self._process.start()
        child.close()
        self._request_id = 0

    def run(self, specs: list[dict[str, Any]], *, timeout: float) -> list[dict[str, Any]]:
        if not self._process.is_alive():
            raise RuntimeError("extension integrity worker is not alive")
        roundtrip_started = time.perf_counter()
        self._request_id += 1
        request_id = self._request_id
        self._connection.send((request_id, specs))
        if not self._connection.poll(timeout):
            raise TimeoutError("extension integrity worker timed out")
        response_id, result, worker_ms = self._connection.recv()
        if response_id != request_id:
            raise RuntimeError("extension integrity worker response mismatch")
        roundtrip_ms = (time.perf_counter() - roundtrip_started) * 1000.0
        perf.record("extension.integrity.worker_roundtrip", roundtrip_ms)
        perf.record("extension.integrity.worker_compute", float(worker_ms))
        perf.record(
            "extension.integrity.worker_outside_compute",
            max(0.0, roundtrip_ms - float(worker_ms)),
        )
        return result

    def close(self, *, force: bool = False) -> None:
        if self._process.is_alive() and not force:
            try:
                self._connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=0.5)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=0.5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=0.5)
        self._connection.close()

    @property
    def process(self) -> multiprocessing.Process:
        return self._process


def _runtime_integrity_worker() -> _RuntimeIntegrityWorker:
    global _RUNTIME_INTEGRITY_WORKER
    with _RUNTIME_INTEGRITY_EXECUTOR_LOCK:
        if _RUNTIME_INTEGRITY_WORKER is None:
            _RUNTIME_INTEGRITY_WORKER = _RuntimeIntegrityWorker()
        return _RUNTIME_INTEGRITY_WORKER


def _reset_runtime_integrity_executor(*, force: bool = False) -> None:
    global _RUNTIME_INTEGRITY_WORKER
    with _RUNTIME_INTEGRITY_EXECUTOR_LOCK:
        worker = _RUNTIME_INTEGRITY_WORKER
        _RUNTIME_INTEGRITY_WORKER = None
    if worker is not None:
        worker.close(force=force)


def shutdown_runtime_integrity_executor() -> None:
    _reset_runtime_integrity_executor()


atexit.register(_reset_runtime_integrity_executor)


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
    integrity = extension_integrity.fingerprint_package(
        _runtime_package_integrity_spec(manifest, str(package_dir))
    )
    if integrity.get("digest") is None:
        raise ExtensionError("runtime package integrity fingerprint failed")
    return {
        "status": "passed",
        "checked_at": _now(),
        "protocol_version": protocol.get("version", _EXTENSION_PROTOCOL_VERSION),
        "required_paths": required_paths,
        "python_modules": python_modules,
        "runtime_package_sha256": integrity["digest"],
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
    _rotate_activation_identity(record)
    if enabled:
        record.pop("quarantine", None)
        record.pop("slow_backend_calls", None)
    elif record.get("quarantine"):
        record.pop("quarantine", None)
    record["updated_at"] = _now()
    _save(data)
    _evict_extension_backend(extension_id)
    extension_instructions.reconcile_blocks(record)
    extension_applied_config.reconcile(record)
    reconcile_runtime_skills()
    reconcile_native_mcp_servers()
    import extension_token_registry
    if bool(enabled):
        if needs_identity_token(record):
            extension_token_registry.mint(extension_id)
    else:
        # Revoke so a disabled extension's token stops authenticating immediately.
        extension_token_registry.revoke(extension_id)
        import ambient_principal
        ambient_principal.registry.revoke_extension(extension_id)
    return record


def _record_backend_incident(
    extension_id: str,
    *,
    activation_id: str,
    elapsed_seconds: float,
    history_key: str,
    reason: str,
    minimum_seconds: float,
) -> list[str]:
    if elapsed_seconds < minimum_seconds:
        return []
    observed_at = time.time()
    cutoff = observed_at - _EXTENSION_SLOW_CALL_WINDOW_SECONDS
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
        history = read_json(_slow_calls_path(), {"extensions": {}})
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
        incidents = [
            float(item) for item in extension_histories.get(history_key, [])
            if isinstance(item, (int, float)) and float(item) >= cutoff
        ]
        incidents.append(observed_at)
        extension_histories[history_key] = incidents
        histories[extension_id] = extension_histories
        write_json(_slow_calls_path(), history)
        if len(incidents) < _EXTENSION_SLOW_CALL_LIMIT:
            return []
        disabled = {extension_id}
        changed = True
        while changed:
            changed = False
            for candidate_id, candidate in data["extensions"].items():
                dependencies = (candidate.get("manifest") or {}).get("dependencies", [])
                if (
                    candidate_id in REQUIRED_EXTENSION_IDS
                    and candidate.get("enabled") is True
                    and disabled.intersection(dependencies)
                ):
                    return []
                if (
                    candidate_id not in disabled
                    and candidate_id not in REQUIRED_EXTENSION_IDS
                    and candidate.get("enabled") is True
                    and disabled.intersection(dependencies)
                ):
                    disabled.add(candidate_id)
                    changed = True
        now = _now()
        cohort = sorted(disabled)
        attributed_generation = _record_generation(data["extensions"][extension_id])
        for candidate_id in disabled:
            candidate = data["extensions"][candidate_id]
            candidate["enabled"] = False
            _rotate_activation_identity(candidate)
            candidate["updated_at"] = now
            candidate["quarantine"] = {
                "reason": reason,
                "at": now,
                "attributed_extension_id": extension_id,
                "attributed_generation": attributed_generation,
                "cohort": cohort,
                "elapsed_seconds": round(float(elapsed_seconds), 3),
            }
        _write_store_unlocked(data)
        histories.pop(extension_id, None)
        write_json(_slow_calls_path(), history)
    for candidate_id in disabled:
        candidate = data["extensions"][candidate_id]
        _evict_extension_backend(candidate_id)
        extension_instructions.reconcile_blocks(candidate)
        extension_applied_config.reconcile(candidate)
        import extension_token_registry
        extension_token_registry.revoke(candidate_id)
    reconcile_runtime_skills()
    reconcile_native_mcp_servers()
    return sorted(disabled)


def record_slow_backend_call(
    extension_id: str,
    *,
    activation_id: str,
    elapsed_seconds: float,
    minimum_seconds: float = EXTENSION_SLOW_CALL_SECONDS,
) -> list[str]:
    return _record_backend_incident(
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        history_key="slow_asgi",
        reason="repeated_slow_backend_calls",
        minimum_seconds=minimum_seconds,
    )


def record_backend_timeout(
    extension_id: str, *, activation_id: str, elapsed_seconds: float
) -> list[str]:
    return _record_backend_incident(
        extension_id,
        activation_id=activation_id,
        elapsed_seconds=elapsed_seconds,
        history_key="timeout",
        reason="repeated_backend_timeouts",
        minimum_seconds=EXTENSION_SLOW_CALL_SECONDS,
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
        manifest = record["manifest"]
        for item in manifest.get("entrypoints", {}).get("skills") or []:
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
    for record in _active_records_from_data(data):
        manifest = record["manifest"]
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


def _assert_runtime_skill_target_available(target: Path, extension_id: str) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if not target.is_symlink() and _runtime_skill_owner(target) == extension_id:
        return
    raise ExtensionError(f"Native skill name {target.name!r} already exists outside this extension")


def _replace_runtime_skill_dir(source: Path, target: Path, extension_id: str) -> None:
    """Swap ``target`` to a fresh copy of ``source`` without a partial-content window.

    Sessions snapshot this directory concurrently (runtime-skill plugin build,
    codex/gemini overlays), so the new tree is staged fully — owner marker
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


def uninstall(extension_id: str) -> None:
    if extension_id in REQUIRED_EXTENSION_IDS:
        raise ExtensionError("Required extension cannot be uninstalled")
    data = _load()
    record = data["extensions"].pop(extension_id, None)
    if not record:
        raise ExtensionError("Extension not installed")
    if extension_id == extension_id_for_role('assistant'):
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


def eligible_native_mcp_launcher_server_configs(
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
        eligible_only=True,
    )


def _mcp_server_configs_for_delivery(
    delivery: str,
    inputs: dict[str, Any],
    *,
    user_facing: bool,
    bare: bool,
    launcher: bool = False,
    eligible_only: bool = False,
) -> dict[str, dict[str, Any]]:
    resolved_inputs = {
        **inputs,
        "open_file_panel_enabled": bool(user_facing),
        "bare_config": bool(bare),
    }
    disabled_extension_ids = _disabled_runtime_extension_ids(inputs)
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
            if delivery == _HARNESS_DELIVERY_NATIVE:
                if eligible_only:
                    if not _native_harness_eligible(record, "mcp", item["name"]):
                        continue
                elif not native_harness_exposed(
                    manifest["id"], "mcp", item["name"], record=record
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
    return {
        **better_agent_runtime_env(),
        **dual_env_many(
            {
                "BETTER_CLAUDE_BACKEND_URL": backend_url,
                "BETTER_CLAUDE_APP_SESSION_ID": str(inputs.get("app_session_id") or ""),
                "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
                "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
                "BETTER_CLAUDE_PROVIDER_ID": str(inputs.get("provider_id") or ""),
                "BETTER_CLAUDE_MODE": str(inputs.get("mode") or ""),
                "BETTER_CLAUDE_WORKING_MODE": str(inputs.get("working_mode") or ""),
                "BETTER_CLAUDE_BARE_CONFIG": "1" if inputs.get("bare_config") else "0",
                "BETTER_CLAUDE_USER_FACING": "1"
                if bool(inputs.get("open_file_panel_enabled"))
                and not bool(inputs.get("bare_config"))
                else "0",
                "BETTER_CLAUDE_DISABLED_BUILTIN_EXTENSIONS": ",".join(disabled_extensions),
                "BETTER_CLAUDE_ACTIVE_CAPABILITY_IDS": ",".join(active_capability_ids),
                "BETTER_CLAUDE_PROVISIONED_TOOL_PROFILE": provisioned_tool_profile,
            }
        ),
    }


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
    if not native_harness_exposed(extension_id, "mcp", server_name, record=record):
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
    base_env = {
        **better_agent_runtime_env(),
        **dual_env_many(
            {
                "BETTER_CLAUDE_BACKEND_URL": backend_url,
                "BETTER_CLAUDE_APP_SESSION_ID": str(inputs.get("app_session_id") or ""),
                "BETTER_CLAUDE_CWD": str(inputs.get("cwd") or ""),
                "BETTER_CLAUDE_MODEL": str(inputs.get("model") or ""),
                "BETTER_CLAUDE_PROVIDER_ID": str(inputs.get("provider_id") or ""),
            }
        ),
    }
    if (
        manifest["id"] == extension_id_for_role('requirements')
        and str(inputs.get("provisioned_tool_profile") or "").strip() == "requirements_processor"
    ):
        base_env.update(dual_env_many({"BETTER_CLAUDE_REQUIREMENTS_PROCESSOR": "1"}))
    ambient_launch = bool((item.get("native_exposure") or {}).get("allowed")) and not str(
        inputs.get("app_session_id") or ""
    ).strip()
    if needs_identity_token(record) and not ambient_launch:
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
    if not is_mcp_server_enabled(manifest["id"], item["name"]):
        return False
    bare = bool(inputs.get("bare_config"))
    user_facing = bool(inputs.get("open_file_panel_enabled")) and not bare
    ambient_native = bool((item.get("native_exposure") or {}).get("allowed")) and not str(
        inputs.get("app_session_id") or ""
    ).strip()
    if (
        item.get("user_facing")
        and not user_facing
        and not ambient_native
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
    launcher_can_mint_token = (
        bool(inputs.get("extension_mcp_launcher_context"))
        and bool(str(inputs.get("app_session_id") or "").strip())
        and bool(explicit_backend_url)
    )
    if (
        item.get("requires_backend_auth")
        and not ambient_native
        and not ((backend_url and internal_token) or launcher_can_mint_token)
    ):
        return False
    predicate = item.get("predicate")
    if predicate and not ambient_native and not _mcp_predicate_matches(predicate, inputs):
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
        if needs_identity_token(record):
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

    _project_ambient_mcp_policy_onto_native_harness()
    settings = _load_ext_settings()
    disabled_extension_ids = set(config_store.get_disabled_builtin_extensions())
    active_records: list[dict[str, Any]] = []
    for record in _active_records():
        extension_id = record["manifest"]["id"]
        if extension_id in disabled_extension_ids or not _record_runtime_ready(record):
            continue
        native_items = [
            item
            for item in _stored_mcp_entrypoints(record)
            if native_harness_exposed(
                extension_id, "mcp", item["name"], settings=settings, record=record
            )
        ]
        if not native_items:
            continue
        native_record = copy.deepcopy(record)
        native_record["manifest"]["entrypoints"]["mcp"] = native_items
        active_records.append(native_record)
    return extension_mcp.reconcile_native_mcp_servers(active_records)


def _project_ambient_mcp_policy_onto_native_harness() -> None:
    # `ambient_mcp_policy_store` (share_all_eligible/excluded_ids) is the one
    # user-facing input for MCP exposure to native providers — it's what the
    # settings UI's "Native MCP sharing" panel and the ambient broker's
    # credential grant both read. Extension-owned "mcp" items' per-item
    # `native_harness` flag (the actual gate `resolve_native_mcp_server_config`
    # consults) is a projection of that policy, kept in sync here on every
    # reconcile rather than mutated directly, so a single edit in the policy
    # store propagates without a second place to update.
    import ambient_mcp_policy_store

    settings = _load_ext_settings()
    changed = False
    for record in _active_records():
        extension_id = record["manifest"]["id"]
        for item in _stored_mcp_entrypoints(record):
            name = item["name"]
            if not _native_harness_eligible(record, "mcp", name):
                continue
            capability_id = f"extension:{extension_id}:{name}"
            desired = ambient_mcp_policy_store.is_exposed(capability_id)
            key = _native_harness_key("mcp", name)
            entry = _ext_settings_entry(settings, extension_id)
            current = key in set(entry["native_harness"])
            if desired == current:
                continue
            exposed = set(entry["native_harness"])
            if desired:
                exposed.add(key)
            else:
                exposed.discard(key)
            entry["native_harness"] = sorted(exposed)
            changed = True
    if changed:
        _save_ext_settings(settings)


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
        "slow_call_grace_seconds": dict(entrypoints.get("slow_call_grace_seconds") or {}),
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
        # Bust browser/PWA caches when an extension changes. Packaged
        # extensions use their installed package revision.
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
                "marketplace_auth": (manifest.get("permissions") or {}).get("marketplace_auth") is True,
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
    for key, value in _projection_cache_items("frontend_entrypoints"):
        if key == (fingerprint, settings_fp):
            return value
    return None


def frontend_entrypoints_cache_key() -> tuple[Any, ...]:
    return (store_fingerprint(), extension_settings_fingerprint())


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
    return str((record.get("source") or {}).get("commit_sha") or "")[:12] or "unversioned"


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
    return (store_fingerprint(), _file_fingerprint(_ui_settings_path()))


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
    policy = item.get("native_exposure") or {}
    return bool(
        policy.get("allowed") is True
        and (
            item.get("requires_backend_auth") is False
            or bool(policy.get("permissions"))
        )
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
        elif kind == "instructions":
            extension_instructions.reconcile_blocks(record)
        elif kind == "mcp":
            reconcile_native_mcp_servers()
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
                elif kind == "instructions":
                    extension_instructions.reconcile_blocks(record)
                elif kind == "mcp":
                    reconcile_native_mcp_servers()
        except Exception:
            pass
        raise ExtensionError(f"Could not apply native exposure: {exc}") from exc
    if kind == "mcp" and not enabled:
        import ambient_principal
        ambient_principal.registry.revoke_extension(extension_id, server_name=name)
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


def _extension_internal_llm_tasks(
    manifest: dict[str, Any],
    extension_tasks: tuple[str, ...],
) -> list[str]:
    tasks = list(extension_tasks)
    for role in manifest.get("core_roles") or []:
        for task in _CORE_ROLE_INTERNAL_LLM_TASKS.get(str(role), ()):
            if task not in tasks:
                tasks.append(task)
    return tasks


def all_internal_llm_task_keys() -> list[str]:
    """Every internal-LLM task key contributed by builtin extensions (public
    and private-registry), in stable declaration order. Absent private
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
    return keys


def internal_llm_task_labels() -> dict[str, str]:
    return {}


def extension_internal_llm_task_keys() -> set[str]:
    task_keys: set[str] = set()
    for keys in _EXTENSION_SETTINGS_INTERNAL_LLM_TASKS.values():
        task_keys.update(keys)
    for keys in _CORE_ROLE_INTERNAL_LLM_TASKS.values():
        task_keys.update(keys)
    return task_keys


def extension_harness_additions(record: dict[str, Any]) -> list[dict[str, Any]]:
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
                "detail": "",
                "native_eligible": True,
                "native_exposed": native_harness_exposed(extension_id, "skill", name, record=record),
            })
    for item in _stored_mcp_entrypoints(record):
        name = str(item.get("name") or "")
        if not name or name in _RESERVED_MCP_SERVER_NAMES:
            continue
        additions.append({
            "kind": "mcp",
            "name": name,
            "detail": "enabled" if is_mcp_server_enabled(str(manifest.get("id") or ""), name) else "disabled",
            "native_eligible": _native_harness_eligible(record, "mcp", name),
            "native_exposed": native_harness_exposed(extension_id, "mcp", name, record=record),
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
