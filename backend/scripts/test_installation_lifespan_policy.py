from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SDK = ROOT / "sdk"


def _child(mode: str, home: Path) -> None:
    os.environ["BETTER_AGENT_HOME"] = str(home)
    os.environ["BETTER_AGENT_TEST_MODE"] = "1"
    sys.path.insert(0, str(BACKEND))
    sys.path.insert(0, str(SDK))
    (home / "runs").mkdir()

    import installation_profile
    import provider_setup

    profile_backend = home / "backend"
    installation_profile.BACKEND_ROOT = profile_backend
    if mode != "setup-required":
        environment = profile_backend / ".venvs" / "test"
        environment.mkdir(parents=True)
        (environment / ".dependency-plan.json").write_text(
            json.dumps({"schema_version": 1, "hash": mode}),
            encoding="utf-8",
        )
        (profile_backend / ".active-venv").write_text(
            ".venvs/test",
            encoding="utf-8",
        )
        launcher = home / ("codex.cmd" if os.name == "nt" else "codex")
        launcher.write_bytes(
            b"@echo off\r\nexit /b 0\r\n"
            if os.name == "nt"
            else b"#!/bin/sh\nexit 0\n"
        )
        launcher.chmod(0o700)
        (home / "config.json").write_text(
            json.dumps({
                "default_provider_id": "codex-id",
                "providers": [{
                    "id": "codex-id",
                    "kind": "codex",
                    "suspended": False,
                }],
            }),
            encoding="utf-8",
        )
        profile = installation_profile.new_active_profile(
            mode=mode,
            provider="codex",
            provider_identity=provider_setup.executable_identity(
                str(launcher.absolute())
            ),
        )
        installation_profile.stage_activation(profile)
        installation_profile.mark_selection_applied()

    import extension_store
    import main
    import requirement_prewarm
    import startup_recovery_gate

    provider_loads: list[str] = []
    integration_calls: list[str] = []

    import installation_admission

    for route in main.app.routes:
        path = str(getattr(route, "path", "") or "")
        scope_type = (
            "websocket"
            if type(route).__name__ == "APIWebSocketRoute"
            else "http"
        )
        capability = installation_admission.capability_for_scope({
            "type": scope_type,
            "path": path,
        })
        assert capability in installation_profile.CAPABILITIES
        if path.startswith("/api/internal/"):
            expected = (
                installation_profile.PROVIDER_CONVERSATIONS
                if path in installation_admission._PROVIDER_INTERNAL_EXACT
                else installation_profile.INTEGRATIONS
            )
            assert capability == expected, path

    def provider_load() -> None:
        provider_loads.append("load")

    async def recover() -> None:
        startup_recovery_gate.mark_recovery_done()

    def integration_call(name: str):
        def call(*_args, **_kwargs):
            integration_calls.append(name)
            if name in ("updates", "extension_update"):
                return {}
            if name in ("reconcile", "readiness"):
                return []
            return 0

        return call

    async def integration_async(name: str):
        integration_calls.append(name)

    async def requirements_projection() -> None:
        await integration_async("requirements_projection")

    async def requirements_processor(_reason: str) -> None:
        await integration_async("requirements_processor")

    async def run() -> None:
        with (
            patch.object(main, "load_all_providers", side_effect=provider_load),
            patch.object(main, "_recover_in_flight_task", side_effect=recover),
            patch.object(
                extension_store,
                "refresh_runtime_readiness_projection",
                side_effect=integration_call("readiness"),
            ),
            patch.object(
                extension_store,
                "check_extension_updates",
                side_effect=integration_call("updates"),
            ),
            patch.object(
                extension_store,
                "list_extensions_with_reconciliation",
                side_effect=integration_call("reconcile"),
            ),
            patch.object(
                extension_store,
                "update_installed_extensions",
                side_effect=integration_call("extension_update"),
            ),
            patch.object(
                extension_store,
                "reconcile_all_instructions",
                side_effect=integration_call("instructions"),
            ),
            patch.object(
                extension_store,
                "reconcile_runtime_skills",
                side_effect=integration_call("skills"),
            ),
            patch.object(
                extension_store,
                "reconcile_native_mcp_servers",
                side_effect=integration_call("native_mcp"),
            ),
            patch.object(
                extension_store,
                "reconcile_extension_tokens",
                side_effect=integration_call("tokens"),
            ),
            patch.object(
                extension_store,
                "reconcile_extension_consent",
                side_effect=integration_call("consent"),
            ),
            patch.object(
                requirement_prewarm,
                "ensure_requirements_projection_ready",
                side_effect=requirements_projection,
            ),
            patch.object(
                requirement_prewarm,
                "run_requirements_prewarm",
                side_effect=requirements_processor,
            ),
        ):
            await main.on_startup()
            assert main._STARTUP_ORCHESTRATOR_TASK is not None
            await main._STARTUP_ORCHESTRATOR_TASK
            await main.on_shutdown()

    asyncio.run(run())

    if mode == "setup-required":
        assert provider_loads == []
    else:
        assert provider_loads == ["load"]
    if mode != installation_profile.DEFAULT:
        assert integration_calls == []


def test_every_profile_lifespan_obeys_runtime_tier() -> None:
    modes = (
        "setup-required",
        "desktop-ui-only",
        "mobile-desktop-ui-only",
        "default",
    )
    for mode in modes:
        with tempfile.TemporaryDirectory(
            prefix=f"ba-install-lifespan-{mode}-"
        ) as tmp:
            subprocess.run(
                [sys.executable, __file__, "--child", mode, tmp],
                check=True,
                cwd=ROOT,
            )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        _child(sys.argv[2], Path(sys.argv[3]))
    else:
        test_every_profile_lifespan_obeys_runtime_tier()
        print("installation lifespan policy tests passed")
