from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import installation_capabilities
from json_store import write_json_durable
from paths import ba_home

SCHEMA_VERSION = 3
RECEIPT_SCHEMA_VERSION = 3
DESKTOP_UI_ONLY = "desktop-ui-only"
MOBILE_DESKTOP_UI_ONLY = "mobile-desktop-ui-only"
DEFAULT = "default"
MODES = frozenset({DESKTOP_UI_ONLY, MOBILE_DESKTOP_UI_ONLY, DEFAULT})
BACKEND_ROOT = Path(__file__).resolve().parent

BOOTSTRAP = "bootstrap"
PROVIDER_CONVERSATIONS = "provider_conversations"
MOBILE = installation_capabilities.MOBILE
INTEGRATIONS = installation_capabilities.INTEGRATIONS
CAPABILITIES = frozenset({
    BOOTSTRAP,
    PROVIDER_CONVERSATIONS,
    MOBILE,
    INTEGRATIONS,
})
NATIVE_PROVIDER_KINDS = frozenset({"openai"})

_IDENTITY_FIELDS = {
    "command",
    "launcher_path",
    "launcher_sha256",
    "target_path",
    "target_sha256",
    "size",
    "mtime_ns",
}
# Fields that identify the executable itself. `size`/`mtime_ns` are recorded as
# provenance but say nothing about identity that the digests do not.
_IDENTITY_MATCH_FIELDS = (
    "command",
    "launcher_path",
    "launcher_sha256",
    "target_path",
    "target_sha256",
)


class InstallationProfileError(ValueError):
    pass


def _path() -> Path:
    return ba_home() / "installation.json"


def _activation_receipt_path() -> Path:
    return ba_home() / "installation-activation.json"


def _inactive(reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "setup_required",
        "reason": reason,
        "generation": None,
        "mode": None,
        "provider": None,
        "provider_identity": None,
    }


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        raise InstallationProfileError("invalid provider executable identity")
    command = value.get("command")
    launcher_path = value.get("launcher_path")
    target_path = value.get("target_path")
    if not all(isinstance(item, str) and item for item in (command, launcher_path, target_path)):
        raise InstallationProfileError("invalid provider executable identity")
    if not Path(launcher_path).is_absolute() or not Path(target_path).is_absolute():
        raise InstallationProfileError("provider executable paths must be absolute")
    for key in ("launcher_sha256", "target_sha256"):
        digest = value.get(key)
        if not isinstance(digest, str) or len(digest) != 64:
            raise InstallationProfileError("invalid provider executable digest")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise InstallationProfileError("invalid provider executable digest") from exc
    for key in ("size", "mtime_ns"):
        if not isinstance(value.get(key), int) or value[key] < 0:
            raise InstallationProfileError("invalid provider executable metadata")
    return dict(value)


def _validate_active(value: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "generation",
        "mode",
        "provider",
        "provider_identity",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise InstallationProfileError("installation profile has unexpected fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InstallationProfileError("unsupported installation profile schema")
    if value.get("status") != "active":
        raise InstallationProfileError("installation profile is not active")
    generation = value.get("generation")
    if not isinstance(generation, str) or not generation:
        raise InstallationProfileError("invalid installation generation")
    mode = value.get("mode")
    if mode not in MODES:
        raise InstallationProfileError("unsupported installation mode")
    provider = value.get("provider")
    if not isinstance(provider, str) or (
        provider not in _installable_provider_kinds()
        and provider not in NATIVE_PROVIDER_KINDS
    ):
        raise InstallationProfileError("unsupported installation provider")
    if provider in NATIVE_PROVIDER_KINDS:
        if value.get("provider_identity") is not None:
            raise InstallationProfileError("native installation provider cannot have an executable identity")
        identity = None
    else:
        identity = _validate_identity(value.get("provider_identity"))
        if identity["command"] != _provider_command(provider):
            raise InstallationProfileError("provider executable identity does not match provider")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "generation": generation,
        "mode": mode,
        "provider": provider,
        "provider_identity": identity,
    }


def _installable_provider_kinds() -> set[str]:
    import provider_manifest

    return set(provider_manifest.installable_kinds())


def _provider_command(provider: str) -> str:
    import provider_setup

    return provider_setup.installer_for(provider).command


_load_cache_lock = threading.Lock()
# (path, fingerprint) -> parsed+validated result. `load()` is called
# unwrapped from many hot async paths (session_manager, orchestrator,
# extension_store, provider adapters) — an mtime+size fingerprint cache
# avoids re-reading and re-parsing installation.json on every call, same
# pattern as models.py/virtual_session_store.py's `_read_cache`.
_load_cache: tuple[Path, tuple[int, int], dict[str, Any]] | None = None


def load() -> dict[str, Any]:
    global _load_cache
    path = _path()
    try:
        stat = path.stat()
    except OSError:
        with _load_cache_lock:
            _load_cache = None
        return _inactive("missing")
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _load_cache_lock:
        if _load_cache is not None and _load_cache[0] == path and _load_cache[1] == fingerprint:
            return copy.deepcopy(_load_cache[2])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        result = _validate_active(value)
    except (OSError, json.JSONDecodeError, InstallationProfileError):
        result = _inactive("invalid")
    with _load_cache_lock:
        _load_cache = (path, fingerprint, result)
    return copy.deepcopy(result)


def require_active() -> dict[str, Any]:
    profile = load()
    if profile["status"] != "active":
        raise InstallationProfileError("installation setup is required")
    return profile


def new_active_profile(
    *,
    mode: str,
    provider: str,
    provider_identity: dict[str, Any],
) -> dict[str, Any]:
    return _validate_active({
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "generation": uuid4().hex,
        "mode": mode,
        "provider": provider,
        "provider_identity": provider_identity,
    })


def new_native_active_profile(*, mode: str, provider: str) -> dict[str, Any]:
    if provider not in NATIVE_PROVIDER_KINDS:
        raise InstallationProfileError("unsupported native installation provider")
    return _validate_active({
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "generation": uuid4().hex,
        "mode": mode,
        "provider": provider,
        "provider_identity": None,
    })


def stage_activation(profile: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_active(profile)
    write_json_durable(_path(), validated)
    _activation_receipt_path().unlink(missing_ok=True)
    return validated


def _assert_active_environment() -> None:
    """Validate the live dependency environment pointer and its plan marker.

    The environment is a rebuildable cache of the profile's dependency plan, so
    it is checked where it lives instead of being pinned into the activation
    receipt: a rebuild must never invalidate a committed installation.

    A frozen bundle carries its dependencies inside the bundle and has no
    pointer to validate — the bundle itself is the environment.
    """
    if getattr(sys, "frozen", False):
        return
    backend = BACKEND_ROOT
    pointer_path = backend / ".active-venv"
    try:
        relative_value = pointer_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InstallationProfileError("backend dependency environment is not active") from exc
    relative = Path(relative_value)
    if not relative_value or relative.is_absolute() or ".." in relative.parts:
        raise InstallationProfileError("backend dependency environment pointer is invalid")
    environment = (backend / relative).resolve()
    venv_root = (backend / ".venvs").resolve()
    if venv_root not in environment.parents:
        raise InstallationProfileError("backend dependency environment pointer escapes its root")
    marker_path = environment / ".dependency-plan.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallationProfileError("backend dependency plan marker is invalid") from exc
    if not isinstance(marker, dict) or marker.get("schema_version") != 1:
        raise InstallationProfileError("backend dependency plan marker is invalid")
    plan_hash = marker.get("hash")
    if not isinstance(plan_hash, str) or not plan_hash:
        raise InstallationProfileError("backend dependency plan marker is invalid")


def _receipt(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generation": profile["generation"],
        "profile_sha256": _canonical_hash(profile),
    }


def mark_selection_applied() -> None:
    profile = require_active()
    _assert_active_environment()
    write_json_durable(_activation_receipt_path(), _receipt(profile))


def refresh_activation_receipt() -> bool:
    """Re-encode a receipt whose commit for the active generation still stands.

    The activation commit belongs to the installation generation, so a receipt
    written in an older encoding is re-stamped in place.
    """
    profile = load()
    if profile["status"] != "active":
        return False
    try:
        prior = json.loads(_activation_receipt_path().read_text(encoding="utf-8"))
        _assert_active_environment()
    except (OSError, json.JSONDecodeError, InstallationProfileError):
        return False
    if not isinstance(prior, dict):
        return False
    if prior.get("generation") != profile["generation"]:
        return False
    if prior.get("profile_sha256") != _canonical_hash(profile):
        return False
    write_json_durable(_activation_receipt_path(), _receipt(profile))
    return True


def _bootstrap_ready(profile: dict[str, Any]) -> bool:
    """Whether setup ran to completion for this installation generation.

    Deliberately independent of the dependency environment and of provider
    selection: rebuilding the environment or changing providers is ordinary
    use, never a revoked installation.

    The commit is identified by generation + profile hash. A receipt written
    in an older encoding still records that commit, so it is honoured as-is —
    a runtime that never re-activates (a frozen bundle, a container) must not
    lose its installation to a change in how receipts are written.
    """
    if profile["status"] != "active":
        return False
    try:
        receipt = json.loads(_activation_receipt_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False
    committed = _receipt(profile)
    return all(
        receipt.get(field) == value
        for field, value in committed.items()
        if field != "schema_version"
    )


def selection_pending() -> bool:
    profile = load()
    return profile["status"] == "active" and not _bootstrap_ready(profile)


def allows(capability: str) -> bool:
    """Whether this process serves the capability.

    Toggleable capabilities read the snapshot captured when the process
    started, so subsystems wired at boot can never disagree with the gate.
    """
    if capability not in CAPABILITIES:
        raise InstallationProfileError("unknown installation capability")
    if capability == BOOTSTRAP:
        return True
    profile = load()
    if not _bootstrap_ready(profile):
        return False
    if capability == PROVIDER_CONVERSATIONS:
        return True
    return installation_capabilities.active(capability, profile["mode"])


def capabilities() -> dict[str, Any]:
    profile = load()
    ready = _bootstrap_ready(profile)
    detail = installation_capabilities.snapshot(profile["mode"]) if ready else {}
    return {
        "status": "active" if ready else "setup_required",
        "setup_required": not ready,
        "mode": profile["mode"] if ready else None,
        "provider_conversations_enabled": ready,
        "mobile_enabled": ready and detail[MOBILE]["active"],
        "integrations_enabled": ready and detail[INTEGRATIONS]["active"],
        "capabilities": detail,
    }


def set_capability_enabled(capability: str, value: bool) -> dict[str, Any]:
    """Record user intent for a toggleable capability."""
    profile = require_active()
    installation_capabilities.set_enabled(capability, value, profile["mode"])
    return capabilities()


def capture_active_capabilities() -> dict[str, bool]:
    """Freeze the capability set this process serves. Called once at startup."""
    return installation_capabilities.capture_active(load()["mode"])


def capability_requested(capability: str) -> bool:
    """User intent for a capability, independent of what this process can serve.

    Provisioning decisions read intent — that is how a newly enabled capability
    gets its dependencies installed. Runtime gates read `allows`.
    """
    profile = load()
    if not _bootstrap_ready(profile):
        return False
    return installation_capabilities.settings(profile["mode"])[capability]


def integrations_enabled() -> bool:
    return allows(INTEGRATIONS)


def mobile_enabled() -> bool:
    return allows(MOBILE)


def provider_conversations_enabled() -> bool:
    return allows(PROVIDER_CONVERSATIONS)


def pinned_provider_executable(command: str) -> tuple[bool, str | None]:
    """Prefer the launcher path setup recorded for the installation's provider.

    The recorded path is the anchor worth keeping: it stops a later PATH entry
    from taking over the provider's command. The recorded digests are
    provenance, not a gate — upgrading the CLI in place is ordinary use, and
    re-pinning happens on the verified setup path. When the recorded launcher
    is gone the command resolves normally, so a reinstall elsewhere can never
    leave the provider reported as missing.
    """
    profile = load()
    if profile["status"] != "active":
        return False, None
    identity = profile["provider_identity"]
    if command != identity["command"] or not _bootstrap_ready(profile):
        return False, None
    launcher = Path(identity["launcher_path"])
    if not launcher.is_file():
        return False, None
    return True, str(launcher)


def repin_provider_executable(identity: dict[str, Any]) -> bool:
    """Record a freshly verified identity for the installation's own provider.

    Accepts new bytes at the recorded launcher path — an in-place upgrade — and
    nothing else. Re-pointing the anchor at a different path would let any
    executable that answers `--version` from an earlier PATH entry inherit the
    pin, which is the takeover the pin exists to prevent. A provider that
    genuinely moved is re-recorded by running setup, not by a status refresh.

    The generation is unchanged — the activation commit still stands — so the
    receipt is re-stamped rather than revoked.
    """
    profile = load()
    if profile["status"] != "active":
        return False
    current = profile["provider_identity"]
    if not isinstance(current, dict):
        return False
    if identity.get("command") != current["command"]:
        return False
    if identity.get("launcher_path") != current["launcher_path"]:
        return False
    if identity == current:
        return False
    was_ready = _bootstrap_ready(profile)
    updated = _validate_active(dict(profile, provider_identity=identity))
    write_json_durable(_path(), updated)
    if was_ready:
        write_json_durable(_activation_receipt_path(), _receipt(updated))
    return True


def executable_identity_matches(identity: dict[str, Any]) -> bool:
    """Whether the recorded provider executable is still byte-identical.

    Compares identity, not incidental file metadata: a byte-identical reinstall
    or a touched mtime is the same executable.
    """
    try:
        expected = _validate_identity(identity)
        from provider_setup import executable_identity

        current = executable_identity(expected["launcher_path"])
    except (OSError, InstallationProfileError, ValueError):
        return False
    return all(current[field] == expected[field] for field in _IDENTITY_MATCH_FIELDS)


def assert_orchestration_mode_allowed(mode: str) -> None:
    normalized = "team" if mode == "manager" else mode
    if normalized not in ("team", "native"):
        raise InstallationProfileError("unsupported orchestration mode")
    if not provider_conversations_enabled():
        raise InstallationProfileError("installation setup is required")
    if normalized == "team" and not integrations_enabled():
        raise InstallationProfileError(
            "team orchestration needs integrations enabled for this installation"
        )
