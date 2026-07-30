"""Real-process proof for provider-auth config generation fencing."""
from __future__ import annotations

import asyncio
import copy
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

TEST_HOME = Path(tempfile.mkdtemp(prefix="provider-auth-generation-fence-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME / "state")
CONTROL = TEST_HOME / "control"
CONTROL.mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import config_store  # noqa: E402
import provider_auth  # noqa: E402
import provider_sync_authority  # noqa: E402
import providers_api  # noqa: E402

CREDENTIALS: dict[str, str] = {}


def _credential_request(action: str, provider_id: str, **kwargs) -> dict:
    if action == "read":
        value = CREDENTIALS.get(provider_id, "")
        return {
            "status": "available" if value else "missing",
            **({"value": value} if value else {}),
        }
    if action == "status":
        return {
            "status": "available" if CREDENTIALS.get(provider_id) else "missing"
        }
    if action == "compare_set":
        current = CREDENTIALS.get(provider_id, "")
        if current != kwargs["expected_value"]:
            return {
                "status": "available" if current else "missing",
                "applied": False,
            }
        value = kwargs["value"]
        if value:
            CREDENTIALS[provider_id] = value
            return {"status": "available", "applied": True}
        CREDENTIALS.pop(provider_id, None)
        return {"status": "missing", "applied": True}
    raise AssertionError(f"unexpected credential action: {action}")


PROBE = """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["BA_AUTH_FENCE_CONTROL"])
selector = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CODEX_HOME")
identity = Path(selector).name if selector else f"default-{sys.argv[1]}"
count_path = root / f"{identity}.count"
count = int(count_path.read_text()) + 1 if count_path.exists() else 1
count_path.write_text(str(count))
(root / f"{identity}-{count}.started").write_text(str(os.getpid()))
deadline = time.monotonic() + 20
while not (root / f"{identity}-{count}.release").exists():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.01)
"""


def _install_probe() -> Path:
    path = TEST_HOME / "claude"
    path.write_text(PROBE)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _add(identity: str) -> dict:
    return config_store.add_provider(
        {
            "name": identity,
            "kind": "claude",
            "mode": "subscription",
            "config_dir": str(TEST_HOME / identity),
        }
    )


def _count(identity: str) -> int:
    path = CONTROL / f"{identity}.count"
    return int(path.read_text()) if path.exists() else 0


def _release(identity: str, count: int = 1) -> None:
    (CONTROL / f"{identity}-{count}.release").touch()


async def _wait_until(predicate, message: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.01)


async def _wait_started(identity: str, count: int = 1) -> None:
    await _wait_until(
        lambda: (CONTROL / f"{identity}-{count}.started").exists(),
        f"{identity} probe {count} did not start",
    )


async def _wait_cached(provider_id: str) -> None:
    await _wait_until(
        lambda: provider_auth._cached_auth(provider_id) is True,
        f"{provider_id} did not publish auth status",
    )


async def _wait_settled(provider_id: str) -> None:
    await _wait_until(
        lambda: provider_id not in provider_auth._pending_refresh,
        f"{provider_id} refresh did not settle",
    )


async def _wait_reconciled() -> None:
    await _wait_until(
        lambda: (
            provider_auth._config_reconcile_task is None
            or provider_auth._config_reconcile_task.done()
        ),
        "provider config reconciliation did not settle",
    )


async def _prepare_interactive(identity: str) -> dict:
    provider = await asyncio.to_thread(_add, identity)
    await _wait_started(identity)
    _release(identity)
    await _wait_cached(provider["id"])
    await _wait_reconciled()
    return provider


async def _wait_flow_gone(provider_id: str) -> None:
    await _wait_until(
        lambda: provider_id not in provider_auth._procs,
        f"{provider_id} interactive process was not released",
    )
    marker = provider_auth._marker_path(provider_id)
    assert marker is None or not marker.exists()


def _authority(provider_id: str) -> tuple[str, int]:
    authority = config_store.provider_record_authority(provider_id)
    assert authority is not None
    return authority


@config_store._serialized_provider_mutation
def _restore_primary_ownership_for_test() -> None:
    state = config_store._load_state()
    state["provider_state_projected"] = False
    config_store._save_state(
        state,
        provider_state_authority=state["provider_state_authority"],
    )


async def _http_patch(
    client: httpx.AsyncClient,
    provider_id: str,
    **changes,
) -> dict:
    generation, revision = _authority(provider_id)
    response = await client.patch(
        f"/api/providers/{provider_id}",
        json={
            **changes,
            "expected_generation": generation,
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _http_delete(
    client: httpx.AsyncClient,
    provider_id: str,
) -> None:
    generation, revision = _authority(provider_id)
    response = await client.request(
        "DELETE",
        f"/api/providers/{provider_id}",
        json={
            "expected_generation": generation,
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text


async def main() -> None:
    probe = _install_probe()
    os.environ["BA_AUTH_FENCE_CONTROL"] = str(CONTROL)
    original_resolve = provider_auth._resolve_binary
    original_catalog_notify = provider_auth._notify_catalog_auth_changed
    catalog_notifications: list[str] = []
    broadcasts: list[str] = []
    start_broadcasts: list[str] = []

    async def broadcast_global(event_type: str, _data: dict) -> None:
        broadcasts.append(event_type)

    async def broadcast_start() -> None:
        start_broadcasts.append("start")

    provider_auth._resolve_binary = lambda _kind: str(probe)
    provider_auth._notify_catalog_auth_changed = catalog_notifications.append
    config_store.credential_session_client.available = lambda: True
    config_store.credential_session_client.request = _credential_request
    initial = config_store.list_providers()["providers"]
    providers_api.configure(broadcast_global)
    app = FastAPI()
    app.include_router(providers_api.router)

    try:
        provider_auth.reopen_status_probes()
        provider_auth.bind_config_change_loop()
        await _wait_started("default-auth")
        await _wait_started("default-login")
        _release("default-auth")
        _release("default-login")
        for record in initial:
            await _wait_cached(record["id"])
        unaffected_id = next(
            record["id"] for record in initial if record["kind"] == "claude"
        )
        unaffected_cache = provider_auth._auth_cache[unaffected_id]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            raced = await asyncio.to_thread(_add, "old-dir")
            await _wait_started("old-dir")
            catalog_notifications.clear()
            await _http_patch(
                client,
                raced["id"],
                config_dir=str(TEST_HOME / "new-dir"),
            )
            _release("old-dir")
            await _wait_started("new-dir")
            assert raced["id"] not in provider_auth._auth_cache
            assert raced["id"] not in catalog_notifications
            _release("new-dir")
            await _wait_cached(raced["id"])
            assert catalog_notifications == [raced["id"]]

            await _http_patch(client, raced["id"], name="unrelated revision")
            await _wait_started("new-dir", 2)
            assert provider_auth._auth_cache[unaffected_id] == unaffected_cache
            _release("new-dir", 2)
            await _wait_cached(raced["id"])

            direct_deleted = await asyncio.to_thread(_add, "direct-delete")
            await _wait_started("direct-delete")
            direct_id = direct_deleted["id"]
            direct_generation, direct_revision = _authority(direct_id)
            deleted = await asyncio.to_thread(
                config_store.delete_provider,
                direct_id,
                expected_generation=direct_generation,
                expected_revision=direct_revision,
            )
            assert deleted == (True, "ok")
            _release("direct-delete")
            await _wait_settled(direct_id)
            assert direct_id not in provider_auth._auth_cache
            assert direct_id not in catalog_notifications

            rest_deleted = await asyncio.to_thread(_add, "rest-delete")
            await _wait_started("rest-delete")
            await _http_delete(client, rest_deleted["id"])
            _release("rest-delete")
            await _wait_settled(rest_deleted["id"])
            assert rest_deleted["id"] not in provider_auth._auth_cache

            blocker = await asyncio.to_thread(_add, "queue-blocker")
            await _wait_started("queue-blocker")
            queued = await asyncio.to_thread(_add, "deleted-before-start")
            await _wait_until(
                lambda: queued["id"] in provider_auth._pending_refresh,
                "queued refresh was not admitted",
            )
            await _http_delete(client, queued["id"])
            _release("queue-blocker")
            await _wait_cached(blocker["id"])
            await _wait_settled(queued["id"])
            assert _count("deleted-before-start") == 0
            assert queued["id"] not in provider_auth._auth_cache

            replaced = await asyncio.to_thread(_add, "sync-old")
            await _wait_started("sync-old")
            payload = config_store.export_provider_sync_state()
            replacement = copy.deepcopy(
                next(
                    record
                    for record in payload["providers"]
                    if record["id"] == replaced["id"]
                )
            )
            replacement["generation"] = str(uuid.uuid4())
            replacement["revision"] = 0
            replacement["config_dir"] = str(TEST_HOME / "sync-new")
            payload["providers"] = [
                replacement if record["id"] == replaced["id"] else record
                for record in payload["providers"]
            ]
            payload["provider_state_authority"] = (
                provider_sync_authority.advance_authority(
                    payload["provider_state_authority"],
                    payload["default_provider_id"],
                    payload["providers"],
                )
            )
            await asyncio.to_thread(config_store.import_provider_sync_state, payload)
            _release("sync-old")
            await _wait_started("sync-new")
            assert replaced["id"] not in provider_auth._auth_cache
            _release("sync-new")
            await _wait_cached(replaced["id"])
            await asyncio.to_thread(_restore_primary_ownership_for_test)

            interactive_direct = await _prepare_interactive("login-direct-delete")
            catalog_notifications.clear()
            broadcasts.clear()
            started = await provider_auth.start_login(
                interactive_direct["id"],
                providers_api._broadcast_provider_changed,
            )
            assert started["ok"] is True
            await _wait_started("login-direct-delete", 2)
            generation, revision = _authority(interactive_direct["id"])
            assert await asyncio.to_thread(
                config_store.delete_provider,
                interactive_direct["id"],
                expected_generation=generation,
                expected_revision=revision,
            ) == (True, "ok")
            await _wait_flow_gone(interactive_direct["id"])
            assert interactive_direct["id"] not in provider_auth._states
            assert interactive_direct["id"] not in provider_auth._auth_cache
            assert interactive_direct["id"] not in catalog_notifications

            registration_race = await _prepare_interactive("registration-race")
            original_apply = config_store.apply_if_provider_matches
            registered = threading.Event()
            resume_start = threading.Event()

            def gated_apply(provider_id, predicate, apply):
                accepted = original_apply(provider_id, predicate, apply)
                if provider_id == registration_race["id"] and accepted:
                    registered.set()
                    assert resume_start.wait(6)
                return accepted

            config_store.apply_if_provider_matches = gated_apply
            broadcasts.clear()
            start_broadcasts.clear()
            catalog_notifications.clear()
            start_task = asyncio.create_task(
                provider_auth.start_login(
                    registration_race["id"],
                    broadcast_start,
                )
            )
            assert await asyncio.to_thread(registered.wait, 6)
            generation, revision = _authority(registration_race["id"])
            assert await asyncio.to_thread(
                config_store.delete_provider,
                registration_race["id"],
                expected_generation=generation,
                expected_revision=revision,
            ) == (True, "ok")
            resume_start.set()
            assert await start_task == {"ok": False, "error": "obsolete"}
            config_store.apply_if_provider_matches = original_apply
            await _wait_flow_gone(registration_race["id"])
            await _wait_settled(registration_race["id"])
            assert registration_race["id"] not in provider_auth._states
            assert registration_race["id"] not in provider_auth._flow_fingerprints
            assert start_broadcasts == []
            assert registration_race["id"] not in catalog_notifications

            cancelled_registration = await _prepare_interactive(
                "registration-cancel"
            )
            registered = threading.Event()
            resume_start = threading.Event()

            def gated_cancel_apply(provider_id, predicate, apply):
                accepted = original_apply(provider_id, predicate, apply)
                if provider_id == cancelled_registration["id"] and accepted:
                    registered.set()
                    assert resume_start.wait(6)
                return accepted

            config_store.apply_if_provider_matches = gated_cancel_apply
            broadcasts.clear()
            start_broadcasts.clear()
            start_task = asyncio.create_task(
                provider_auth.start_login(
                    cancelled_registration["id"],
                    broadcast_start,
                )
            )
            assert await asyncio.to_thread(registered.wait, 6)
            start_task.cancel()
            await asyncio.sleep(0)
            assert not start_task.done()
            resume_start.set()
            try:
                await start_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled registration completed")
            config_store.apply_if_provider_matches = original_apply
            await _wait_flow_gone(cancelled_registration["id"])
            assert cancelled_registration["id"] not in provider_auth._states
            assert (
                cancelled_registration["id"]
                not in provider_auth._flow_fingerprints
            )
            assert start_broadcasts == []

            interactive_rest = await _prepare_interactive("logout-rest-delete")
            catalog_notifications.clear()
            broadcasts.clear()
            started = await provider_auth.start_logout(
                interactive_rest["id"],
                providers_api._broadcast_provider_changed,
            )
            assert started["ok"] is True
            await _wait_started("logout-rest-delete", 2)
            await _http_delete(client, interactive_rest["id"])
            await _wait_flow_gone(interactive_rest["id"])
            assert interactive_rest["id"] not in provider_auth._states
            assert interactive_rest["id"] not in provider_auth._auth_cache
            assert interactive_rest["id"] not in catalog_notifications

            interactive_sync = await _prepare_interactive("login-sync-old")
            catalog_notifications.clear()
            broadcasts.clear()
            started = await provider_auth.start_login(
                interactive_sync["id"],
                providers_api._broadcast_provider_changed,
            )
            assert started["ok"] is True
            await _wait_started("login-sync-old", 2)
            payload = config_store.export_provider_sync_state()
            replacement = copy.deepcopy(
                next(
                    record
                    for record in payload["providers"]
                    if record["id"] == interactive_sync["id"]
                )
            )
            replacement["generation"] = str(uuid.uuid4())
            replacement["revision"] = 0
            replacement["config_dir"] = str(TEST_HOME / "login-sync-new")
            payload["providers"] = [
                replacement
                if record["id"] == interactive_sync["id"]
                else record
                for record in payload["providers"]
            ]
            payload["provider_state_authority"] = (
                provider_sync_authority.advance_authority(
                    payload["provider_state_authority"],
                    payload["default_provider_id"],
                    payload["providers"],
                )
            )
            await asyncio.to_thread(config_store.import_provider_sync_state, payload)
            await _wait_flow_gone(interactive_sync["id"])
            await _wait_started("login-sync-new")
            assert interactive_sync["id"] not in provider_auth._states
            assert interactive_sync["id"] not in catalog_notifications
            _release("login-sync-new")
            await _wait_cached(interactive_sync["id"])
            await asyncio.to_thread(_restore_primary_ownership_for_test)

            fail_closed = await _prepare_interactive("fingerprint-failure")
            original_fingerprint = provider_auth._auth_fingerprint
            provider_auth._auth_fingerprint = lambda _provider: (_ for _ in ()).throw(
                RuntimeError("forced fingerprint failure")
            )
            generation, revision = _authority(fail_closed["id"])
            await asyncio.to_thread(
                config_store.update_provider,
                fail_closed["id"],
                {"name": "fingerprint failure committed"},
                expected_generation=generation,
                expected_revision=revision,
            )
            provider_auth._auth_fingerprint = original_fingerprint
            assert provider_auth._cached_auth(fail_closed["id"]) is None
            assert provider_auth.login_state(fail_closed["id"])["status"] == "idle"

        assert broadcasts
    finally:
        await provider_auth.shutdown_status_probes()
        provider_auth.shutdown_all()
        provider_auth._resolve_binary = original_resolve
        provider_auth._notify_catalog_auth_changed = original_catalog_notify
        shutil.rmtree(TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    print("provider auth generation fence integration test passed")
