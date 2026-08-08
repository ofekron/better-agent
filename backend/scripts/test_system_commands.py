#!/usr/bin/env python3
"""Unit coverage for backend/system_commands.py's `_SystemCommandPortImpl`
(ADR 0011 System & Host Surface command plane) — specifically the
behaviors this migration pass changed: `save_harness_profile`'s switch
from `apply_override_patch` to the SAME `harness_field_writer.
apply_field_writes` the legacy `PATCH .../fields` route calls, typed
rejection codes (`not_found`/`invalid_field`/`stale_revision`/`invalid`),
`delete_harness_profile`'s previously-ignored `revision` check,
`set_installation_capability`'s confirm-required 409 + missing fact
publish, and `sync_node_providers`'s credential-sync-only scoping.

Isolated via `paths.engage_test_home` before any backend import (no real
`~/.better-claude` touched) — same idiom `test_adapter_system.py` uses.

Run:
    ./scripts/run-backend-tests.sh -- scripts/test_system_commands.py -q
    PYTHONPATH=.:./backend:./sdk python3 backend/scripts/test_system_commands.py
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-system-commands-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import _test_installation  # noqa: E402  (bare — backend/scripts sibling)
import system_commands  # noqa: E402  (bare — matches main.py/adapter_api.py's own import style; a dotted `backend.system_commands` import needs boundary-test alias coverage this stateless module has no reason to carry)

build_system_command_port = system_commands.build_system_command_port


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# save_harness_profile
# ---------------------------------------------------------------------------

def test_save_harness_profile_create_calls_create_profile() -> None:
    port = build_system_command_port()
    result = _run(port.save_harness_profile(None, {"name": "My Profile"}, None, ()))
    assert result.accepted, result
    assert result.ref, result

    import harness_profile_store

    profile = harness_profile_store.get_profile(result.ref)
    assert profile is not None
    assert profile["name"] == "My Profile"


def test_save_harness_profile_write_uses_field_writer_not_override_patch() -> None:
    """`apply_field_writes` (NOT `apply_override_patch` directly) is the
    SAME function the legacy `PATCH .../fields` route calls — proven here
    by writing a `disabled_builtin_tools` field write (a shape
    `apply_override_patch` alone cannot resolve; it needs
    `harness_field_writer`'s absolute-to-delta conversion)."""
    port = build_system_command_port()
    created = _run(port.save_harness_profile(None, {"name": "Writable"}, None, ()))
    assert created.accepted, created
    profile_id = created.ref

    import harness_profile_store

    before = harness_profile_store.get_profile(profile_id)
    result = _run(port.save_harness_profile(
        profile_id, {}, before["revision"],
        ({"path": ["disabled_builtin_tools", "some_tool"], "value": True},),
    ))
    assert result.accepted, result
    after = harness_profile_store.get_profile(profile_id)
    assert after["revision"] != before["revision"]


def test_save_harness_profile_stale_revision_is_typed() -> None:
    port = build_system_command_port()
    created = _run(port.save_harness_profile(None, {"name": "Stale"}, None, ()))
    profile_id = created.ref

    result = _run(port.save_harness_profile(
        profile_id, {}, "not-the-real-revision",
        ({"path": ["disabled_builtin_tools", "some_tool"], "value": True},),
    ))
    assert not result.accepted
    assert result.code == "stale_revision", result


def test_save_harness_profile_not_found_is_typed() -> None:
    port = build_system_command_port()
    result = _run(port.save_harness_profile(
        "no-such-profile", {}, None,
        ({"path": ["disabled_builtin_tools", "some_tool"], "value": True},),
    ))
    assert not result.accepted
    assert result.code == "not_found", result


def test_save_harness_profile_default_is_blocked() -> None:
    port = build_system_command_port()
    result = _run(port.save_harness_profile(
        "default", {}, None,
        ({"path": ["disabled_builtin_tools", "some_tool"], "value": True},),
    ))
    assert not result.accepted
    assert result.code == "invalid", result


def test_save_harness_profile_empty_writes_is_rejected() -> None:
    port = build_system_command_port()
    created = _run(port.save_harness_profile(None, {"name": "EmptyWrites"}, None, ()))
    result = _run(port.save_harness_profile(created.ref, {}, None, ()))
    assert not result.accepted
    assert result.code == "invalid", result


# ---------------------------------------------------------------------------
# delete_harness_profile
# ---------------------------------------------------------------------------

def test_delete_harness_profile_stale_revision_is_rejected_and_not_deleted() -> None:
    """Root-cause proof of the pre-existing bug this pass fixed:
    `system_commands.py` used to call `delete_profile(id, None)`,
    silently ignoring ANY caller-supplied revision. This test fails on
    the pre-fix code (a stale revision would be accepted and the profile
    deleted) and passes after (deletion is rejected, profile survives)."""
    port = build_system_command_port()
    created = _run(port.save_harness_profile(None, {"name": "ToDelete"}, None, ()))
    profile_id = created.ref

    result = _run(port.delete_harness_profile(profile_id, "not-the-real-revision"))
    assert not result.accepted
    assert result.code == "stale_revision", result

    import harness_profile_store

    assert harness_profile_store.get_profile(profile_id) is not None


def test_delete_harness_profile_correct_revision_succeeds() -> None:
    port = build_system_command_port()
    created = _run(port.save_harness_profile(None, {"name": "ToDelete2"}, None, ()))
    profile_id = created.ref

    import harness_profile_store

    revision = harness_profile_store.get_profile(profile_id)["revision"]
    result = _run(port.delete_harness_profile(profile_id, revision))
    assert result.accepted, result
    assert harness_profile_store.get_profile(profile_id) is None


def test_delete_harness_profile_default_is_blocked() -> None:
    port = build_system_command_port()
    result = _run(port.delete_harness_profile("default", None))
    assert not result.accepted
    assert result.code == "invalid", result


# ---------------------------------------------------------------------------
# set_installation_capability
# ---------------------------------------------------------------------------

def test_set_installation_capability_requires_confirm_to_disable_integrations() -> None:
    _test_installation.activate(Path(paths.ba_home()))
    port = build_system_command_port()

    result = _run(port.set_installation_capability("integrations", False, False))
    assert not result.accepted
    assert result.code == "409", result


def test_set_installation_capability_confirmed_disable_succeeds() -> None:
    _test_installation.activate(Path(paths.ba_home()))
    port = build_system_command_port()

    result = _run(port.set_installation_capability("integrations", False, True))
    assert result.accepted, result


def test_set_installation_capability_enable_needs_no_confirm() -> None:
    _test_installation.activate(Path(paths.ba_home()))
    port = build_system_command_port()

    result = _run(port.set_installation_capability("mobile", True, False))
    assert result.accepted, result


def test_set_installation_capability_publishes_fact_for_live_push() -> None:
    """Root-cause proof: the v2 command-port path used to mutate state via
    `installation_profile.set_capability_enabled` WITHOUT publishing the
    `installation.fire.capability_changed` fact `system_adapter.py`'s live
    push depends on — a capability change submitted via the v2 intent
    never reached a connected `/ws/v2/surface` client. Fails pre-fix (no
    fact seen), passes post-fix."""
    _test_installation.activate(Path(paths.ba_home()))
    from backend.event_bus import BusEvent, bus

    received: list[BusEvent] = []

    async def _listener(event: BusEvent) -> None:
        received.append(event)

    async def _run_with_listener():
        bus.subscribe(
            "installation.fire.capability_changed", _listener,
            name="test_set_installation_capability_publishes_fact_for_live_push",
        )
        try:
            port = build_system_command_port()
            result = await port.set_installation_capability("mobile", True, False)
            assert result.accepted, result
        finally:
            bus.unsubscribe("test_set_installation_capability_publishes_fact_for_live_push")

    asyncio.run(_run_with_listener())
    assert received, "expected installation.fire.capability_changed to be published"
    assert received[0].payload.get("capability_id") == "mobile"


def test_set_installation_capability_unknown_capability_is_404() -> None:
    port = build_system_command_port()
    result = _run(port.set_installation_capability("not-a-real-capability", True, False))
    assert not result.accepted
    assert result.code == "404", result


# ---------------------------------------------------------------------------
# sync_node_providers
# ---------------------------------------------------------------------------

def test_sync_node_providers_rejects_the_non_secret_shape_as_unsupported() -> None:
    port = build_system_command_port()
    result = _run(port.sync_node_providers("worker-1", False, ()))
    assert not result.accepted
    assert result.code == "unsupported", result


def test_sync_node_providers_rejects_empty_provider_ids_even_with_include_secrets() -> None:
    port = build_system_command_port()
    result = _run(port.sync_node_providers("worker-1", True, ()))
    assert not result.accepted
    assert result.code == "unsupported", result


def test_sync_node_providers_credential_path_rejects_unknown_node() -> None:
    """`include_secrets=True` + `provider_ids` routes to
    `node_provider_credential_sync.authorize_and_sync`, which requires an
    APPROVED node — an unapproved node_id is a real (typed) rejection,
    not a crash, proving the call reaches the real function rather than
    the old `authorize_and_sync(node_id, None)` call (which always raised
    before even reaching node-lookup)."""
    port = build_system_command_port()
    result = _run(port.sync_node_providers("no-such-node", True, ("provider-1",)))
    assert not result.accepted
    assert result.code == "409", result


def test_sync_node_providers_credential_path_forwards_selected_provider_ids() -> None:
    """Root-cause proof of the pre-existing bug this pass fixed: the old
    code called `authorize_and_sync(node_id, None)` unconditionally, which
    `_selected_providers(None)` always rejects before even reaching
    node/provider lookup — `sync_node_providers` could never succeed
    regardless of what the caller asked for. Patches `authorize_and_sync`
    itself (isolating this from node-approval/connection setup, which
    `test_sync_node_providers_credential_path_rejects_unknown_node` above
    already covers end-to-end) to assert the REAL `provider_ids` list
    reaches it, not a hard-coded `None`."""
    import node_provider_credential_sync

    captured: dict[str, object] = {}

    async def _fake_authorize_and_sync(node_id: str, provider_ids: object) -> dict:
        captured["node_id"] = node_id
        captured["provider_ids"] = provider_ids
        return {"node_id": node_id, "provider_credentials": []}

    original = node_provider_credential_sync.authorize_and_sync
    node_provider_credential_sync.authorize_and_sync = _fake_authorize_and_sync
    try:
        port = build_system_command_port()
        result = _run(port.sync_node_providers("worker-1", True, ("provider-1", "provider-2")))
    finally:
        node_provider_credential_sync.authorize_and_sync = original

    assert result.accepted, result
    assert captured["node_id"] == "worker-1"
    assert captured["provider_ids"] == ["provider-1", "provider-2"]


_TESTS = [
    test_save_harness_profile_create_calls_create_profile,
    test_save_harness_profile_write_uses_field_writer_not_override_patch,
    test_save_harness_profile_stale_revision_is_typed,
    test_save_harness_profile_not_found_is_typed,
    test_save_harness_profile_default_is_blocked,
    test_save_harness_profile_empty_writes_is_rejected,
    test_delete_harness_profile_stale_revision_is_rejected_and_not_deleted,
    test_delete_harness_profile_correct_revision_succeeds,
    test_delete_harness_profile_default_is_blocked,
    test_set_installation_capability_requires_confirm_to_disable_integrations,
    test_set_installation_capability_confirmed_disable_succeeds,
    test_set_installation_capability_enable_needs_no_confirm,
    test_set_installation_capability_publishes_fact_for_live_push,
    test_set_installation_capability_unknown_capability_is_404,
    test_sync_node_providers_rejects_the_non_secret_shape_as_unsupported,
    test_sync_node_providers_rejects_empty_provider_ids_even_with_include_secrets,
    test_sync_node_providers_credential_path_rejects_unknown_node,
    test_sync_node_providers_credential_path_forwards_selected_provider_ids,
]


def _run_standalone() -> None:
    failures = 0
    for test in _TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    if failures:
        print(f"{failures}/{len(_TESTS)} tests failed")
        sys.exit(1)
    print(f"all {len(_TESTS)} tests passed")


if __name__ == "__main__":
    _run_standalone()
