"""Single source for making the first-party sdk/ importable in spawned processes.

The backend and its runners import `better_agent_sdk` from the checkout's
sdk/ directory (a bare package, not an installed distribution). Every
process launcher must inject it into PYTHONPATH through this helper:
provider runner env, the dev browser-backend supervisor, and the packaged
desktop supervisor. The PyInstaller bundle covers it at build time via
`pathex` in BetterAgent.spec.
"""

from __future__ import annotations

import os
from pathlib import Path


def sdk_pythonpath(checkout: Path, existing: str = "") -> str:
    """PYTHONPATH value with the checkout's sdk/ prepended.

    Returns `existing` unchanged when sdk/ is absent (frozen bundle dirs)
    or already present in the path.
    """
    sdk = checkout / "sdk"
    if not sdk.is_dir():
        return existing
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    if str(sdk) in entries:
        return existing
    return str(sdk) + (os.pathsep + existing if existing else "")


def apply_sdk_pythonpath(env: dict[str, str], checkout: Path) -> dict[str, str]:
    """Set PYTHONPATH in `env` (in place) so sdk/ is importable; returns env."""
    value = sdk_pythonpath(checkout, env.get("PYTHONPATH", ""))
    if value:
        env["PYTHONPATH"] = value
    return env
