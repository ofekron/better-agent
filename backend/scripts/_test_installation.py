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


def provider_identity(
    root: Path,
    provider: str,
    launcher_path: str | None = None,
) -> dict[str, Any]:
    """Identity for the profile's pinned provider executable.

    Defaults to a stub launcher, which is what deterministic tests want. Live
    tests that actually spawn the vendor CLI must pass the real binary:
    `cli_paths.resolve_cli_binary` returns the pinned path for this command,
    so a stub here would silently no-op every real run.
    """
    import provider_setup

    if launcher_path:
        return provider_setup.executable_identity(str(Path(launcher_path).absolute()))

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
    launcher_path: str | None = None,
) -> dict[str, Any]:
    import config_store
    import installation_profile
    import provider_sync_authority

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
    provider_record = config_store._new_provider_record(provider)
    provider_record["id"] = provider_id
    providers = [provider_record]
    profile_record = config_store._seed_profile_for_provider(provider_record)
    (root / "config.json").write_text(
        json.dumps({
            "schema_version": config_store.CONFIG_SCHEMA_VERSION,
            "default_provider_id": provider_id,
            "providers": providers,
            "runtime_profiles": [profile_record],
            "default_runtime_profile_id": profile_record["id"],
            "provider_state_authority": provider_sync_authority.new_authority(
                provider_id,
                providers,
            ),
            "provider_state_projected": False,
        }),
        encoding="utf-8",
    )
    profile = installation_profile.new_active_profile(
        mode=mode,
        provider=provider,
        provider_identity=provider_identity(root, provider, launcher_path),
    )
    installation_profile.stage_activation(profile)
    installation_profile.mark_selection_applied()
    assert not installation_profile.selection_pending()
    return profile


def default_llm_assignment(provider_id: str | None = None) -> dict[str, str]:
    """An internal_llm_assignments entry for a provider, reading default model
    and reasoning effort from its runtime profile.

    The v3 config schema moved default_model/default_reasoning_effort off the
    provider record and onto its runtime profile, so callers must not read
    `provider["default_model"]`. This is the single test-side source for that
    lookup; assignment-seeding fixtures route through it instead of forking the
    read in every file. ``provider_id`` defaults to the first listed provider.
    """
    import config_store

    providers = config_store.list_providers()["providers"]
    pid = provider_id or providers[0]["id"]
    profile = next(
        p for p in config_store.list_runtime_profiles() if p["provider_id"] == pid
    )
    return {
        "provider_id": pid,
        "model": profile["default_model"],
        "reasoning_effort": profile.get("default_reasoning_effort") or "",
    }
