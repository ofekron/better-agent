from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import asyncio
import json
import shutil

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ask-fork-route-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import config_store  # noqa: E402
import extension_store  # noqa: E402
import main  # noqa: E402


def _install_team_orchestration_extension() -> None:
    extension_id = extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
    package = Path(_TMP_HOME) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["default_session"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


_install_team_orchestration_extension()


def test_ask_fork_missing_target_session_fails_before_delegation():
    async def fail_run_delegation(*_args, **_kwargs):
        raise AssertionError("missing ask-fork target entered delegation")

    original = main.coordinator.run_delegation
    original_get = main.session_manager.get
    main.coordinator.run_delegation = fail_run_delegation
    main.session_manager.get = lambda _sid: (_ for _ in ()).throw(
        AssertionError("ask-fork existence validation must not deepcopy via get()")
    )
    try:
        try:
            asyncio.run(main.internal_ask_fork(
                {
                    "app_session_id": "caller-session",
                    "instructions": "check this",
                    "worker_session_id": "missing-target-session",
                    "worker_description": "",
                    "model": "model",
                    "cwd": "/tmp",
                    "run_mode": "fork",
                },
                x_internal_token=main.coordinator.internal_token,
            ))
        except HTTPException as exc:
            error = exc
        else:
            raise AssertionError("missing ask-fork target did not raise")
    finally:
        main.coordinator.run_delegation = original
        main.session_manager.get = original_get

    assert error.status_code == 404
    assert error.detail == main.t("error.session_not_found")


def test_ask_fork_existing_target_enters_delegation():
    called: list[dict] = []

    async def fake_run_delegation(**kwargs):
        called.append(kwargs)
        return {"success": True}

    target = main.session_manager.create(
        name="target",
        cwd="/tmp",
        orchestration_mode="native",
        model="model",
        source="test",
    )
    original = main.coordinator.run_delegation
    original_get = main.session_manager.get
    main.coordinator.run_delegation = fake_run_delegation
    main.session_manager.get = lambda _sid: (_ for _ in ()).throw(
        AssertionError("ask-fork existence validation must not deepcopy via get()")
    )
    body = {
        "app_session_id": "caller-session",
        "instructions": "check this",
        "worker_session_id": target["id"],
        "worker_description": "",
        "provider_id": "provider-1",
        "model": "model",
        "reasoning_effort": "high",
        "cwd": "/tmp",
        "run_mode": "fork",
    }
    try:
        result = asyncio.run(main.internal_ask_fork(
            body,
            x_internal_token=main.coordinator.internal_token,
        ))
    finally:
        main.coordinator.run_delegation = original
        main.session_manager.get = original_get

    assert result == {"success": True}
    assert called[0]["worker_session_id"] == target["id"]
    assert called[0]["provider_id"] == "provider-1"
    assert called[0]["reasoning_effort"] == "high"
    assert called[0]["include_events"] is False


def test_ask_fork_include_events_is_explicit():
    called: list[dict] = []

    async def fake_run_delegation(**kwargs):
        called.append(kwargs)
        return {"success": True, "events": [{"type": "agent_message"}]}

    target = main.session_manager.create(
        name="target-events",
        cwd="/tmp",
        orchestration_mode="native",
        model="model",
        source="test",
    )
    original = main.coordinator.run_delegation
    main.coordinator.run_delegation = fake_run_delegation
    try:
        result = asyncio.run(main.internal_ask_fork(
            {
                "app_session_id": "caller-session",
                "instructions": "check this",
                "worker_session_id": target["id"],
                "worker_description": "",
                "model": "model",
                "cwd": "/tmp",
                "run_mode": "fork",
                "include_events": True,
            },
            x_internal_token=main.coordinator.internal_token,
        ))
    finally:
        main.coordinator.run_delegation = original

    assert result["success"] is True
    assert result["events"] == [{"type": "agent_message"}]
    assert called[0]["include_events"] is True


def test_ask_fork_locked_runner_accepts_provider_config():
    source = (BACKEND / "orchs" / "manager" / "_delegation.py").read_text(encoding="utf-8")
    signature_start = source.index("async def run_delegation_locked(")
    signature_end = source.index(") -> dict:", signature_start)
    signature = source[signature_start:signature_end]
    assert "provider_id: str = \"\"" in signature
    assert "reasoning_effort: str = \"\"" in signature

    call_start = source.index("return await run_delegation_locked(")
    call_end = source.index("\n    finally:", call_start)
    call = source[call_start:call_end]
    assert "provider_id=provider_id" in call
    assert "reasoning_effort=reasoning_effort" in call


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
