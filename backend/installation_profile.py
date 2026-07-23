from __future__ import annotations

import json
from typing import Any

from json_store import write_json
from paths import ba_home

SCHEMA_VERSION = 2
DESKTOP_UI_ONLY = "desktop-ui-only"
MOBILE_DESKTOP_UI_ONLY = "mobile-desktop-ui-only"
DEFAULT = "default"
MODES = frozenset({DESKTOP_UI_ONLY, MOBILE_DESKTOP_UI_ONLY, DEFAULT})


class InstallationProfileError(ValueError):
    pass


def _path():
    return ba_home() / "installation.json"


def _selection_marker_path():
    return ba_home() / "installation-selection-applied.json"


def _default() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "mode": DEFAULT, "provider": None}


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InstallationProfileError("installation profile must be an object")
    if set(value) != {"schema_version", "mode", "provider"}:
        raise InstallationProfileError("installation profile has unexpected fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InstallationProfileError("unsupported installation profile schema")
    mode = value.get("mode")
    if mode not in MODES:
        raise InstallationProfileError("unsupported installation mode")
    provider = value.get("provider")
    if provider is not None:
        if not isinstance(provider, str) or provider not in _installable_provider_kinds():
            raise InstallationProfileError("unsupported installation provider")
    return {"schema_version": SCHEMA_VERSION, "mode": mode, "provider": provider}


def _installable_provider_kinds() -> set[str]:
    import provider_manifest

    return set(provider_manifest.installable_kinds())


def load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return _default()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallationProfileError(f"invalid installation profile: {exc}") from exc
    return _validate(value)


def save(*, mode: str, provider: str) -> dict[str, Any]:
    profile = _validate({
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "provider": provider,
    })
    write_json(_path(), profile)
    _selection_marker_path().unlink(missing_ok=True)
    return profile


def selection_pending() -> bool:
    profile = load()
    if profile["provider"] is None:
        return False
    marker_path = _selection_marker_path()
    if not marker_path.exists():
        return True
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return marker != {"profile": profile}


def mark_selection_applied() -> None:
    profile = load()
    if profile["provider"] is None:
        _selection_marker_path().unlink(missing_ok=True)
        return
    write_json(_selection_marker_path(), {"profile": profile})


def capabilities() -> dict[str, Any]:
    mode = load()["mode"]
    return {
        "mode": mode,
        "mobile_enabled": mode != DESKTOP_UI_ONLY,
        "integrations_enabled": mode == DEFAULT,
    }


def integrations_enabled() -> bool:
    return capabilities()["integrations_enabled"]


def mobile_enabled() -> bool:
    return capabilities()["mobile_enabled"]


def assert_orchestration_mode_allowed(mode: str) -> None:
    normalized = "team" if mode == "manager" else mode
    if normalized not in ("team", "native"):
        raise InstallationProfileError("unsupported orchestration mode")
    if normalized == "team" and not integrations_enabled():
        raise InstallationProfileError(
            "team orchestration is unavailable in UI-only installation modes"
        )
