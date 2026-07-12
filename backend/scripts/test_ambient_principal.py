from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ambient_principal import AmbientPrincipalRegistry
import coordination


def test_registry_is_memory_scoped_ttl_bound_and_permission_scoped() -> None:
    now = [100.0]
    registry = AmbientPrincipalRegistry(clock=lambda: now[0])
    token, principal = registry.issue(
        extension_id="ofek.extension",
        server_name="tools",
        permissions=["coordination.lock_ops"],
        os_user_id="501",
        ttl_seconds=5,
    )
    assert registry.resolve(token) == principal
    assert registry.resolve(token, permission="coordination.lock_ops") == principal
    assert registry.resolve(token, permission="sessions.write") is None
    now[0] = 106.0
    assert registry.resolve(token) is None


def test_revoke_is_principal_and_extension_scoped() -> None:
    registry = AmbientPrincipalRegistry()
    first_token, first = registry.issue(
        extension_id="ofek.extension", server_name="first", permissions=[], os_user_id="501"
    )
    second_token, second = registry.issue(
        extension_id="ofek.extension", server_name="second", permissions=[], os_user_id="501"
    )
    assert registry.revoke(first.principal_id) == first
    assert registry.resolve(first_token) is None
    assert registry.resolve(second_token) == second
    assert registry.revoke_extension("ofek.extension", server_name="second") == [second]
    assert registry.resolve(second_token) is None


async def test_coordination_uses_principal_identity_and_disconnect_cleanup() -> None:
    coordination._locks.clear()
    first = {"principal_id": "ambient-a", "principal_extension_id": "ofek.extension"}
    second = {"principal_id": "ambient-b", "principal_extension_id": "ofek.extension"}
    acquired = await coordination.lock_ops(key="file_edit:/repo/a", owner=first)
    assert acquired["success"] is True
    assert (await coordination.lock_ops(key="file_edit:/repo/a", op="reattach", owner=second))["success"] is False
    assert (await coordination.lock_ops(key="file_edit:/repo/a", op="reattach", owner=first))["success"] is True
    assert await coordination.release_principal_locks("ambient-b") == []
    assert await coordination.release_principal_locks("ambient-a") == ["file_edit:/repo/a"]
    assert not coordination._locks


if __name__ == "__main__":
    test_registry_is_memory_scoped_ttl_bound_and_permission_scoped()
    test_revoke_is_principal_and_extension_scoped()
    asyncio.run(test_coordination_uses_principal_identity_and_disconnect_cleanup())
    print("all ambient principal tests passed")
