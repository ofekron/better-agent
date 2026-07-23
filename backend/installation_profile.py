from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from json_store import write_json_durable
from paths import ba_home, bc_home

SCHEMA_VERSION = 3
RECEIPT_SCHEMA_VERSION = 1
DESKTOP_UI_ONLY = "desktop-ui-only"
MOBILE_DESKTOP_UI_ONLY = "mobile-desktop-ui-only"
DEFAULT = "default"
MODES = frozenset({DESKTOP_UI_ONLY, MOBILE_DESKTOP_UI_ONLY, DEFAULT})
BACKEND_ROOT = Path(__file__).resolve().parent

BOOTSTRAP = "bootstrap"
PROVIDER_CONVERSATIONS = "provider_conversations"
MOBILE = "mobile"
INTEGRATIONS = "integrations"
CAPABILITIES = frozenset({
    BOOTSTRAP,
    PROVIDER_CONVERSATIONS,
    MOBILE,
    INTEGRATIONS,
})

_IDENTITY_FIELDS = {
    "command",
    "launcher_path",
    "launcher_sha256",
    "target_path",
    "target_sha256",
    "size",
    "mtime_ns",
}


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
    if not isinstance(provider, str) or provider not in _installable_provider_kinds():
        raise InstallationProfileError("unsupported installation provider")
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


def load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return _inactive("missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return _validate_active(value)
    except (OSError, json.JSONDecodeError, InstallationProfileError):
        return _inactive("invalid")


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


def stage_activation(profile: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_active(profile)
    write_json_durable(_path(), validated)
    _activation_receipt_path().unlink(missing_ok=True)
    return validated


def _active_environment_receipt() -> dict[str, str]:
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
    plan_hash = marker.get("hash") if isinstance(marker, dict) else None
    if marker.get("schema_version") != 1 or not isinstance(plan_hash, str) or not plan_hash:
        raise InstallationProfileError("backend dependency plan marker is invalid")
    return {"active_env": relative.as_posix(), "dependency_plan_hash": plan_hash}


def _provider_selection_fingerprint(provider: str) -> str:
    path = bc_home() / "config.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallationProfileError("provider selection is not persisted") from exc
    providers = state.get("providers") if isinstance(state, dict) else None
    default_id = state.get("default_provider_id") if isinstance(state, dict) else None
    if not isinstance(providers, list) or not isinstance(default_id, str):
        raise InstallationProfileError("provider selection is not persisted")
    selected = next(
        (
            item
            for item in providers
            if isinstance(item, dict) and item.get("id") == default_id
        ),
        None,
    )
    if selected is None or selected.get("kind") != provider or selected.get("suspended") is True:
        raise InstallationProfileError("provider selection does not match installation")
    projection = {
        "default_provider_id": default_id,
        "providers": [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "suspended": item.get("suspended") is True,
            }
            for item in providers
            if isinstance(item, dict)
        ],
    }
    return _canonical_hash(projection)


def mark_selection_applied() -> None:
    profile = require_active()
    environment = _active_environment_receipt()
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generation": profile["generation"],
        "profile_sha256": _canonical_hash(profile),
        "provider_selection_sha256": _provider_selection_fingerprint(profile["provider"]),
        **environment,
    }
    write_json_durable(_activation_receipt_path(), receipt)


def _activation_ready(profile: dict[str, Any]) -> bool:
    if profile["status"] != "active":
        return False
    try:
        receipt = json.loads(_activation_receipt_path().read_text(encoding="utf-8"))
        environment = _active_environment_receipt()
        selection_hash = _provider_selection_fingerprint(profile["provider"])
    except (OSError, json.JSONDecodeError, InstallationProfileError):
        return False
    expected = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generation": profile["generation"],
        "profile_sha256": _canonical_hash(profile),
        "provider_selection_sha256": selection_hash,
        **environment,
    }
    return receipt == expected


def selection_pending() -> bool:
    profile = load()
    return profile["status"] == "active" and not _activation_ready(profile)


def allows(capability: str) -> bool:
    if capability not in CAPABILITIES:
        raise InstallationProfileError("unknown installation capability")
    if capability == BOOTSTRAP:
        return True
    profile = load()
    if not _activation_ready(profile):
        return False
    mode = profile["mode"]
    if capability == PROVIDER_CONVERSATIONS:
        return True
    if capability == MOBILE:
        return mode != DESKTOP_UI_ONLY
    return mode == DEFAULT


def capabilities() -> dict[str, Any]:
    profile = load()
    ready = _activation_ready(profile)
    mode = profile["mode"] if ready else None
    return {
        "status": "active" if ready else "setup_required",
        "setup_required": not ready,
        "mode": mode,
        "provider_conversations_enabled": ready,
        "mobile_enabled": ready and mode != DESKTOP_UI_ONLY,
        "integrations_enabled": ready and mode == DEFAULT,
    }


def integrations_enabled() -> bool:
    return allows(INTEGRATIONS)


def mobile_enabled() -> bool:
    return allows(MOBILE)


def provider_conversations_enabled() -> bool:
    return allows(PROVIDER_CONVERSATIONS)


def pinned_provider_executable(command: str) -> tuple[bool, str | None]:
    profile = load()
    if profile["status"] != "active":
        return False, None
    identity = profile["provider_identity"]
    if command != identity["command"]:
        return False, None
    if not _activation_ready(profile) or not executable_identity_matches(identity):
        return True, None
    return True, identity["launcher_path"]


def executable_identity_matches(identity: dict[str, Any]) -> bool:
    try:
        expected = _validate_identity(identity)
        from provider_setup import executable_identity

        return executable_identity(expected["launcher_path"]) == expected
    except (OSError, InstallationProfileError, ValueError):
        return False


def assert_orchestration_mode_allowed(mode: str) -> None:
    normalized = "team" if mode == "manager" else mode
    if normalized not in ("team", "native"):
        raise InstallationProfileError("unsupported orchestration mode")
    if not provider_conversations_enabled():
        raise InstallationProfileError("installation setup is required")
    if normalized == "team" and not integrations_enabled():
        raise InstallationProfileError(
            "team orchestration is unavailable in UI-only installation modes"
        )
