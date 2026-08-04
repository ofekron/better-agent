from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import credential_session_recovery as recovery  # noqa: E402
import ops_api  # noqa: E402
from credential_session_client import CredentialSessionRestartRequired  # noqa: E402


def test_credential_transport_failure_requests_supervised_restart(
    monkeypatch,
) -> None:
    restart_requested = False

    async def request_restart() -> bool:
        nonlocal restart_requested
        restart_requested = True
        return True

    def resolve() -> object:
        raise CredentialSessionRestartRequired("credential session request timed out")

    monkeypatch.setattr(recovery, "_request_restart", request_restart)

    with pytest.raises(CredentialSessionRestartRequired):
        asyncio.run(recovery.resolve_provider_with_restart(resolve))

    assert restart_requested


def test_non_transport_provider_failure_does_not_restart(monkeypatch) -> None:
    async def request_restart() -> bool:
        raise AssertionError("ordinary provider failure must not restart the backend")

    def resolve() -> object:
        raise RuntimeError("provider configuration is invalid")

    monkeypatch.setattr(recovery, "_request_restart", request_restart)

    with pytest.raises(RuntimeError, match="provider configuration is invalid"):
        asyncio.run(recovery.resolve_provider_with_restart(resolve))


def test_successful_provider_resolution_does_not_restart(monkeypatch) -> None:
    expected = object()

    async def request_restart() -> bool:
        raise AssertionError("successful provider resolution must not restart")

    monkeypatch.setattr(recovery, "_request_restart", request_restart)

    result = asyncio.run(recovery.resolve_provider_with_restart(lambda: expected))

    assert result is expected


def test_supervised_restart_requests_are_coalesced(monkeypatch) -> None:
    restart_ids: list[str] = []
    restart_entered = asyncio.Event()
    release_restart = asyncio.Event()

    async def trigger_restart(request_id: str) -> None:
        restart_ids.append(request_id)
        restart_entered.set()
        await release_restart.wait()

    async def exercise() -> list[bool]:
        first = asyncio.create_task(ops_api.request_supervised_backend_restart())
        await restart_entered.wait()
        second = asyncio.create_task(ops_api.request_supervised_backend_restart())
        await asyncio.sleep(0)
        release_restart.set()
        return await asyncio.gather(first, second)

    monkeypatch.setenv("BETTER_CLAUDE_RUN_SH_SUPERVISOR", "1")
    monkeypatch.setattr(ops_api, "_supervised_restart_requested", False)
    monkeypatch.setattr(ops_api, "_trigger_supervisor_restart", trigger_restart)

    assert asyncio.run(exercise()) == [True, True]
    assert len(restart_ids) == 1
