"""User-editable capability settings.

The installation profile records what setup *started* with; this store records
what the user currently wants enabled. Install-time mode only seeds the first
read — it never constrains what can be enabled later.

Three distinct facts, deliberately not collapsed:

* ``enabled``   — user intent, persisted here, changeable at any time.
* ``provisioned`` — the running interpreter can actually serve the capability.
* ``active``    — the snapshot taken when the process started. Every runtime
  gate reads this, so a capability can never be half-applied: subsystems are
  wired once at boot from a value that cannot move under them.

``enabled != active`` is what the UI reports as "restart to apply".
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any

from json_store import write_json_durable
from paths import ba_home

SCHEMA_VERSION = 1

MOBILE = "mobile"
INTEGRATIONS = "integrations"
TOGGLEABLE = (MOBILE, INTEGRATIONS)

# Import probes proving the running interpreter can serve a capability.
# Integrations need no dependency beyond the base set.
_PROBES: dict[str, tuple[str, ...]] = {
    MOBILE: ("firebase_admin",),
    INTEGRATIONS: (),
}

_active: dict[str, bool] | None = None


class InstallationCapabilityError(ValueError):
    pass


def _path():
    return ba_home() / "installation-capabilities.json"


def _seed(mode: str | None) -> dict[str, bool]:
    """Derive the first-read defaults from the install-time mode."""
    import installation_profile

    return {
        MOBILE: mode != installation_profile.DESKTOP_UI_ONLY,
        INTEGRATIONS: mode == installation_profile.DEFAULT,
    }


def _stored() -> dict[str, bool] | None:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    if set(capabilities) != set(TOGGLEABLE):
        return None
    if not all(isinstance(item, bool) for item in capabilities.values()):
        return None
    return dict(capabilities)


def settings(seed_mode: str | None) -> dict[str, bool]:
    """Current user intent, seeded from ``seed_mode`` until first written."""
    return _stored() or _seed(seed_mode)


def enabled(capability: str, seed_mode: str | None) -> bool:
    _assert_toggleable(capability)
    return settings(seed_mode)[capability]


def set_enabled(capability: str, value: bool, seed_mode: str | None) -> dict[str, bool]:
    _assert_toggleable(capability)
    state = settings(seed_mode)
    state[capability] = bool(value)
    write_json_durable(
        _path(),
        {"schema_version": SCHEMA_VERSION, "capabilities": state},
    )
    return state


def _assert_toggleable(capability: str) -> None:
    if capability not in TOGGLEABLE:
        raise InstallationCapabilityError(f"capability is not toggleable: {capability}")


def provisioned(capability: str) -> bool:
    """Whether this process can actually serve the capability right now."""
    _assert_toggleable(capability)
    for module in _PROBES[capability]:
        try:
            if importlib.util.find_spec(module) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def self_provisionable() -> bool:
    """Whether this runtime can install what a newly enabled capability needs.

    A frozen bundle carries a fixed dependency set, so enabling a capability it
    was not built with is a build-time concern, not a restart away.
    """
    return not getattr(sys, "frozen", False)


def in_app_restart_supported() -> bool:
    """Whether the runtime can restart itself when asked.

    Only the run.sh supervisor can; a container or a bare uvicorn is restarted
    from outside, and offering a button that cannot work would misreport it.
    """
    return os.environ.get("BETTER_CLAUDE_RUN_SH_SUPERVISOR") == "1"


def capture_active(seed_mode: str | None) -> dict[str, bool]:
    """Freeze the capability set this process runs with."""
    global _active
    current = settings(seed_mode)
    _active = {
        capability: current[capability] and provisioned(capability)
        for capability in TOGGLEABLE
    }
    return dict(_active)


def active(capability: str, seed_mode: str | None) -> bool:
    _assert_toggleable(capability)
    if _active is None:
        capture_active(seed_mode)
    return _active[capability]


def snapshot(seed_mode: str | None) -> dict[str, Any]:
    current = settings(seed_mode)
    if _active is None:
        capture_active(seed_mode)
    can_provision = self_provisionable()
    can_restart = in_app_restart_supported()
    return {
        capability: {
            "enabled": current[capability],
            "provisioned": provisioned(capability),
            "active": _active[capability],
            "restart_required": current[capability] != _active[capability],
            "self_provisionable": can_provision,
            "in_app_restart_supported": can_restart,
        }
        for capability in TOGGLEABLE
    }
