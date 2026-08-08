"""Backend side of the extension daemons surface.

Two responsibilities, matching the two lifecycles:

1. ``publish_registry()`` — project the desired supervisor-daemon set to
   ``ba_home()/daemons/registry.json`` for the platform daemon host (see the
   top-level ``daemonhost`` package). The backend owns extension state and
   publishes facts; the host decides what to run. An entry is removed only
   when its extension record is explicitly uninstalled or disabled — an
   extension whose package is missing from the active checkout keeps its
   entry untouched, so switching lines can never uninstall the daemon that
   executed the switch.

2. ``reconcile_backend_daemons()`` — spawn/stop ``lifecycle: "backend"``
   daemons as supervised children of this backend process. They get the same
   scrubbed environment supervisor daemons get (never auth tokens).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover (import-time sys.path bootstrap; backend is already on sys.path in every runtime/test entry)
    sys.path.insert(0, str(_REPO_ROOT))  # pragma: no cover

from daemonhost.host import scrubbed_env  # noqa: E402
from daemonhost.jsonio import read_json, write_json  # noqa: E402
from daemonhost.paths import daemon_root, registry_path, state_path  # noqa: E402

import extension_store  # noqa: E402

_lock = threading.Lock()
_reconcile_lock = threading.RLock()
_backend_procs: dict[str, subprocess.Popen] = {}
_BUILTIN_SWITCH_MANIFEST = _REPO_ROOT / "extensions" / "switch-control" / "better-agent-extension.json"


def _daemon_key(extension_id: str, name: str) -> str:
    return f"{extension_id}:{name}"


def _is_drained(key: str, entry: dict[str, Any]) -> bool:
    generation = str(entry.get("generation") or "")
    if not generation:
        return False
    path = daemon_root(str(entry["extension_id"]), str(entry["name"])) / "lifecycle.json"
    try:
        payload = read_json(path)
    except OSError:
        return False
    return (
        payload.get("generation") == generation
        and payload.get("state") == "drained"
    )


def _drain_or_remove(entries: dict[str, Any], key: str) -> None:
    existing = entries.get(key)
    if not isinstance(existing, dict):
        return
    if existing.get("retire_policy") != "drain" or _is_drained(key, existing):
        entries.pop(key, None)
        return
    entries[key] = {**existing, "desired_state": "draining"}


def _declared_daemons() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Yields (extension_id, record, spec); the record id lives on its manifest."""
    triples: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for record in extension_store.list_extensions(include_hidden=True):
        manifest = record.get("manifest") or {}
        extension_id = str(manifest.get("id") or "")
        if not extension_id:
            continue
        if extension_id == extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID:
            manifest = extension_store.validate_manifest(
                json.loads(_BUILTIN_SWITCH_MANIFEST.read_text(encoding="utf-8"))
            )
        for spec in (manifest.get("entrypoints") or {}).get("daemons") or []:
            triples.append((extension_id, record, spec))
    return triples


def publish_registry() -> dict[str, Any]:
    import installation_profile

    if not installation_profile.integrations_enabled():
        write_json(registry_path(), {"daemons": {}})
        return {}
    existing = read_json(registry_path()).get("daemons")
    entries: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    declared = _declared_daemons()
    known_ids = {
        str((record.get("manifest") or {}).get("id") or "")
        for record in extension_store.list_extensions(include_hidden=True)
    }
    desired_keys: dict[str, set[str]] = {}
    available_ids: set[str] = set()
    for extension_id, record, spec in declared:
        known_ids.add(extension_id)
        key = _daemon_key(extension_id, spec["name"])
        if spec.get("lifecycle") != "supervisor":
            continue
        desired_keys.setdefault(extension_id, set()).add(key)
        if not extension_store.is_extension_active(extension_id):
            _drain_or_remove(entries, key)
            continue
        source_root = extension_store.runtime_package_root(extension_id)
        if source_root is None:
            # Package unavailable on this line: keep whatever the host has.
            continue
        available_ids.add(extension_id)
        source_root = extension_store.supervisor_daemon_package_root(extension_id, source_root)
        generation = str((entries.get(key) or {}).get("generation") or uuid.uuid4().hex)
        entries[key] = {
            "extension_id": extension_id,
            "name": spec["name"],
            "module": spec["module"],
            "lifecycle": "supervisor",
            "retire_policy": spec.get("retire_policy") or "immediate",
            "desired_state": "active",
            "generation": generation,
            "restart_policy": spec.get("restart_policy") or {},
            "env_allowlist": spec.get("env_allowlist") or [],
            "ports": spec.get("ports") or [],
            "source_root": str(source_root),
        }
    # Drop entries whose extension record no longer exists at all (explicit
    # uninstall). Records merely missing their package on this line were
    # yielded by list_extensions and are in known_ids, so they survive.
    for key in list(entries):
        extension_id = str(entries[key].get("extension_id") or "")
        if extension_id in available_ids and key not in desired_keys.get(extension_id, set()):
            _drain_or_remove(entries, key)
            continue
        if extension_id not in known_ids and extension_store.get_extension(extension_id) is None:
            _drain_or_remove(entries, key)
    write_json(registry_path(), {"daemons": entries})
    return entries


def reconcile_backend_daemons() -> None:
    desired: dict[str, dict[str, Any]] = {}
    import installation_profile
    integrations_enabled = installation_profile.integrations_enabled()
    for extension_id, record, spec in _declared_daemons():
        if (
            not integrations_enabled
            or
            spec.get("lifecycle") != "backend"
            or not extension_store.is_extension_active(extension_id)
        ):
            continue
        if not extension_store.is_extension_runtime_ready(extension_id):
            continue
        source_root = extension_store.runtime_package_root(extension_id)
        if source_root is None:
            continue
        desired[_daemon_key(extension_id, spec["name"])] = {**spec, "source_root": str(source_root)}
    with _lock:
        for key, proc in list(_backend_procs.items()):
            if key not in desired or proc.poll() is not None:
                _stop(proc)
                del _backend_procs[key]
        for key, spec in desired.items():
            if key in _backend_procs:
                continue
            env = scrubbed_env(spec.get("env_allowlist") or [])
            env["PYTHONPATH"] = spec["source_root"]
            env["BETTER_AGENT_DAEMON"] = key
            try:
                _backend_procs[key] = subprocess.Popen(
                    [sys.executable, "-m", spec["module"]],
                    cwd=spec["source_root"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                )
            except OSError:
                continue


def shutdown_backend_daemons() -> None:
    with _lock:
        for proc in _backend_procs.values():
            _stop(proc)
        _backend_procs.clear()


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def reconcile() -> None:
    """Project each durable extension-store mutation into daemon state."""
    with _reconcile_lock:
        publish_registry()
        reconcile_backend_daemons()


def daemons_projection() -> dict[str, Any]:
    """Read model for the UI: desired set + host-owned live status."""
    with _lock:
        backend_status = {
            key: {"status": "running" if proc.poll() is None else "exited", "pid": proc.pid}
            for key, proc in _backend_procs.items()
        }
    return {
        "registry": read_json(registry_path()).get("daemons") or {},
        "supervisor_state": read_json(state_path()),
        "backend_daemons": backend_status,
    }


def ui_only_quiescent() -> bool:
    registry = read_json(registry_path()).get("daemons")
    supervisor = read_json(state_path()).get("daemons")
    with _lock:
        backend_running = any(proc.poll() is None for proc in _backend_procs.values())
    supervisor_running = False
    for value in supervisor.values() if isinstance(supervisor, dict) else ():
        if not isinstance(value, dict) or not isinstance(value.get("pid"), int):
            continue
        try:
            os.kill(value["pid"], 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        supervisor_running = True
        break
    return not registry and not backend_running and not supervisor_running


extension_store.subscribe_store_mutations("extension_daemons", reconcile)
