"""Locks the `daemons` extension surface: manifest validation, per-lifecycle
permission gating, reserved-port refusal, smoke-test coverage, and the
registry-publish semantics (entries survive a line whose checkout lacks the
extension; explicit uninstall removes them).

Run: backend/.venv/bin/python backend/scripts/test_extension_daemons_surface.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import paths

_TMP = tempfile.mkdtemp(prefix="ba-daemons-surface-")
paths.engage_test_home(_TMP)

import extension_store
import extension_daemons
from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import registry_path


def _manifest(daemons, permissions=None, protocol=None):
    manifest = {
        "kind": "better-agent-extension",
        "id": "test.daemons",
        "name": "Daemons Test",
        "version": "0.1.0",
        "surfaces": ["daemons"],
        "entrypoints": {"daemons": daemons},
        "permissions": permissions if permissions is not None else {"daemons": "supervisor"},
    }
    if protocol is not None:
        manifest["protocol"] = protocol
    return manifest


def _rejects(manifest, needle):
    try:
        extension_store.validate_manifest(manifest)
    except extension_store.ExtensionError as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc}"
        return
    raise AssertionError(f"manifest unexpectedly valid (wanted {needle!r})")


DAEMON = {"name": "worker", "module": "daemon.worker", "lifecycle": "supervisor"}

# Valid manifest round-trips with defaults applied.
validated = extension_store.validate_manifest(_manifest([DAEMON]))
entry = validated["entrypoints"]["daemons"][0]
assert entry["restart_policy"] == {"max_restarts": 5, "backoff_seconds": 5}
assert entry["env_allowlist"] == [] and entry["ports"] == []
# Daemon modules are required smoke coverage (default protocol derivation).
assert "daemon.worker" in validated["protocol"]["smoke_test"]["python_modules"]

# Permission gating.
_rejects(_manifest([DAEMON], permissions={}), "permissions.daemons")
_rejects(
    _manifest([DAEMON], permissions={"daemons": "backend"}),
    "supervisor-lifecycle daemons require",
)
_rejects(_manifest([DAEMON], permissions={"daemons": True}), "'backend' or 'supervisor'")

# Surface must be declared.
no_surface = _manifest([DAEMON])
no_surface["surfaces"] = []
_rejects(no_surface, "requires the 'daemons' surface")

# Shape validation.
_rejects(_manifest([{**DAEMON, "lifecycle": "forever"}]), "lifecycle")
_rejects(_manifest([{**DAEMON, "ports": [8000]}]), "reserved")
_rejects(_manifest([{**DAEMON, "ports": [80]}]), "1024..65535")
_rejects(_manifest([{**DAEMON, "env_allowlist": ["bad-key"]}]), "invalid env keys")
_rejects(_manifest([{**DAEMON, "restart_policy": {"max_restarts": 1000}}]), "max_restarts")
_rejects(_manifest([DAEMON, DAEMON]), "duplicate")
_rejects(_manifest([{"name": "x", "lifecycle": "backend"}]), "require a module")

# Explicit protocol must cover the daemon module.
_rejects(
    _manifest(
        [DAEMON],
        protocol={"version": 1, "smoke_test": {"python_modules": []}},
    ),
    "daemon.worker",
)

# --- registry publish semantics -------------------------------------------
records = {}


def _fake_list_extensions(include_hidden=False):
    return list(records.values())


def _fake_get_extension(extension_id):
    return records.get(extension_id)


def _fake_runtime_package_root(extension_id):
    return (records.get(extension_id) or {}).get("_root")


extension_daemons.extension_store.list_extensions = _fake_list_extensions
extension_daemons.extension_store.get_extension = _fake_get_extension
extension_daemons.extension_store.runtime_package_root = _fake_runtime_package_root

source_root = Path(_TMP) / "ext-src"
source_root.mkdir(parents=True)
# Real store records carry the id ONLY on the manifest (regression: a
# top-level "id" here once masked a startup KeyError in publish_registry).
records["test.daemons"] = {
    "enabled": True,
    "entitlement": {"status": "not_required"},
    "manifest": extension_store.validate_manifest(_manifest([DAEMON])),
    "_root": source_root,
}

entries = extension_daemons.publish_registry()
key = "test.daemons:worker"
assert key in entries and entries[key]["source_root"] == str(source_root)

switch_manifest = json.loads(
    (BACKEND.parent / "extensions" / "switch-control" / "better-agent-extension.json").read_text(
        encoding="utf-8"
    )
)
validated_switch_manifest = extension_store.validate_manifest(switch_manifest)
smoke = extension_store._run_extension_smoke_test(
    validated_switch_manifest,
    BACKEND.parent / "extensions" / "switch-control",
)
assert smoke["status"] == "passed"
records[extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID] = {
    "enabled": True,
    "entitlement": {"status": "not_required"},
    "manifest": validated_switch_manifest,
    "_root": BACKEND.parent / "extensions" / "switch-control",
}
entries = extension_daemons.publish_registry()
switch_key = f"{extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID}:line-switch"
assert entries[switch_key]["source_root"] == str(BACKEND.parent / "switch_control_daemon")

records[extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID]["manifest"] = {
    **validated_switch_manifest,
    "version": "0.1.0",
    "entrypoints": {
        **validated_switch_manifest["entrypoints"],
        "daemons": [{"name": "switcher", "module": "daemon.switcher", "lifecycle": "supervisor"}],
    },
}
write_json(registry_path(), {"daemons": {
    f"{extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID}:switcher": {
        "extension_id": extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID,
        "name": "switcher",
        "module": "daemon.switcher",
        "lifecycle": "supervisor",
        "source_root": str(BACKEND.parent / "extensions" / "switch-control"),
    },
}})
entries = extension_daemons.publish_registry()
assert switch_key in entries, "bundled switch daemon must override a stale installed manifest"
assert f"{extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID}:switcher" not in entries
del records[extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID]

# Package missing on this line (root None): entry survives untouched.
records["test.daemons"]["_root"] = None
entries = extension_daemons.publish_registry()
assert key in entries, "entry must survive a line whose checkout lacks the package"

# Disabled: entry removed.
records["test.daemons"]["enabled"] = False
entries = extension_daemons.publish_registry()
assert key not in entries, "disable must remove the registry entry"

# Re-enable, then full uninstall (record gone): entry removed.
records["test.daemons"]["enabled"] = True
records["test.daemons"]["_root"] = source_root
assert key in extension_daemons.publish_registry()
del records["test.daemons"]
assert key not in extension_daemons.publish_registry(), "uninstall must remove the entry"

# Foreign entries (unknown writer) with a live record are preserved by merge.
write_json(registry_path(), {"daemons": {"other.ext:d": {"extension_id": "other.ext", "lifecycle": "supervisor"}}})
records["other.ext"] = {
    "enabled": True,
    "entitlement": {"status": "not_required"},
    "manifest": {"id": "other.ext", "entrypoints": {}},
    "_root": None,
}
assert "other.ext:d" in extension_daemons.publish_registry()

print("OK test_extension_daemons_surface")
