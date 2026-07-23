from __future__ import annotations

import json
from typing import Any

from json_store import write_json
from paths import ba_home

SCHEMA_VERSION = 1
MODES = frozenset({"default", "ui-only"})


class InstallationProfileError(ValueError):
    pass


def _path():
    return ba_home() / "installation.json"


def _default() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "mode": "default", "provider": None}


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
    return profile


def integrations_enabled() -> bool:
    return load()["mode"] == "default"
