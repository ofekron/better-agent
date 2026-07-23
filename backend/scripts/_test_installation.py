"""Shared fixture: activate a real installation inside an isolated test home.

Callers must have engaged an isolated home (via `_test_home`) and put backend/
on sys.path BEFORE importing this module. `activate()` produces the same state
a completed installer run leaves behind: an active profile, a committed
activation receipt, a live dependency-environment pointer, and a provider
selection persisted in config.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def provider_identity(root: Path, provider: str) -> dict[str, Any]:
    import provider_setup

    command = provider_setup.installer_for(provider).command
    suffix = ".cmd" if os.name == "nt" else ""
    launcher = root / f"{command}{suffix}"
    launcher.write_bytes(
        b"@echo off\r\nexit /b 0\r\n" if suffix else b"#!/bin/sh\nexit 0\n"
    )
    launcher.chmod(0o700)
    return provider_setup.executable_identity(str(launcher.absolute()))


def activate(
    root: Path,
    mode: str | None = None,
    provider: str = "claude",
) -> dict[str, Any]:
    import installation_profile

    mode = mode or installation_profile.DEFAULT
    backend = root / "backend"
    installation_profile.BACKEND_ROOT = backend
    environment = backend / ".venvs" / "test"
    environment.mkdir(parents=True, exist_ok=True)
    (environment / ".dependency-plan.json").write_text(
        json.dumps({"schema_version": 1, "hash": f"{mode}-{provider}"}),
        encoding="utf-8",
    )
    (backend / ".active-venv").write_text(".venvs/test", encoding="utf-8")
    provider_id = f"{provider}-id"
    (root / "config.json").write_text(
        json.dumps({
            "default_provider_id": provider_id,
            "providers": [
                {
                    "id": provider_id,
                    "kind": provider,
                    "suspended": False,
                }
            ],
        }),
        encoding="utf-8",
    )
    profile = installation_profile.new_active_profile(
        mode=mode,
        provider=provider,
        provider_identity=provider_identity(root, provider),
    )
    installation_profile.stage_activation(profile)
    installation_profile.mark_selection_applied()
    assert not installation_profile.selection_pending()
    return profile
