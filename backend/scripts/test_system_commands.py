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
import importlib
import shutil
import sys
import tempfile
from contextlib import contextmanager
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


_MISSING = object()


@contextmanager
def _patch(module_name, attr, fake):
    """Single home for module-attribute monkeypatching in this file.

    The port methods import their dependencies lazily inside the function
    body (`import extension_store` etc.), so patching the module attribute
    is read at call time — no need to reach into the port internals.
    """
    mod = importlib.import_module(module_name)
    original = getattr(mod, attr, _MISSING)
    setattr(mod, attr, fake)
    try:
        yield
    finally:
        if original is _MISSING:
            delattr(mod, attr)
        else:
            setattr(mod, attr, original)


def _recorder(bucket):
    def _rec(*args, **kwargs):
        bucket.append((args, kwargs))
    return _rec


def _async_recorder(bucket):
    async def _rec(*args, **kwargs):
        bucket.append((args, kwargs))
    return _rec


# ---------------------------------------------------------------------------
# set_default_harness_profile
# ---------------------------------------------------------------------------

def test_set_default_harness_profile_is_unsupported() -> None:
    """No legacy mutation exists for a global default (module docstring) —
    every call rejects with `code="unsupported"`."""
    port = build_system_command_port()
    result = _run(port.set_default_harness_profile("some-profile"))
    assert not result.accepted
    assert result.code == "unsupported", result


# ---------------------------------------------------------------------------
# update_extension_config
# ---------------------------------------------------------------------------

# section -> (store setter attr, patch body, expected setter call args, broadcast topic(s))
_SIMPLE_SECTIONS = [
    ("settings", "set_extension_setting", {"a": 1},
     ("ext-1", "a", 1), ("extension.config.settings",)),
    ("instructions", "set_user_instructions", {"instructions": "do X"},
     ("ext-1", "do X"), ("extension.config.instructions",)),
    ("ui_settings", "set_ui_settings", {"quick_button_enabled": True, "page_enabled": False},
     ("ext-1",), ("extension.config.ui_settings", "extension.ui")),
    ("permissions", "set_permission_grant", {"permission": "P", "granted": True},
     ("ext-1", "P", True), ("extension.config.permissions",)),
    ("mcp", "set_mcp_server_enabled", {"server_name": "S", "enabled": True},
     ("ext-1", "S", True), ("extension.config.mcp",)),
    ("skills", "set_runtime_skill_enabled", {"skill_name": "K", "enabled": True},
     ("ext-1", "K", True), ("extension.config.skills",)),
]


def test_update_extension_config_simple_sections_dispatch_and_broadcast() -> None:
    """Each simple section routes the patch body to the matching store
    setter, then broadcasts its topic — proves the dispatcher table
    (lines 58-107) rather than touching it blindly."""
    for section, setter, patch_body, expect_args, topic in _SIMPLE_SECTIONS:
        setter_calls: list = []
        broadcasts: list = []
        with _patch("extension_store", setter, _recorder(setter_calls)), \
                _patch("extension_api", "_broadcast_extension_changed", _async_recorder(broadcasts)):
            port = build_system_command_port()
            result = _run(port.update_extension_config("ext-1", section, dict(patch_body)))
        assert result.accepted, (section, result)
        assert setter_calls, (section, "setter not invoked")
        assert setter_calls[0][0] == expect_args, (section, setter_calls[0][0])
        assert broadcasts[0][0] == topic, (section, broadcasts[0][0])


def test_update_extension_config_internal_llm_merges_owned_assignments() -> None:
    """internal_llm keeps every task NOT owned by this extension at its
    current assignment, overlays the caller's (validated) assignments,
    and persists the merge — covers lines 74-90."""
    record = {"id": "ext-1"}
    set_calls: list = []
    broadcasts: list = []

    with _patch("extension_store", "get_extension", lambda eid: record), \
            _patch("extension_store", "extension_internal_llm_tasks", lambda _r: ["owned-task"]), \
            _patch("config_store", "get_internal_llm_assignments",
                   lambda: {"other-task": "model-a", "owned-task": "model-old"}), \
            _patch("config_store", "set_internal_llm_assignments", _recorder(set_calls)), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder(broadcasts)):
        port = build_system_command_port()
        result = _run(port.update_extension_config(
            "ext-1", "internal_llm", {"assignments": {"owned-task": "model-new"}},
        ))
    assert result.accepted, result
    merged = set_calls[0][0][0]  # first positional arg to set_internal_llm_assignments
    assert merged == {"other-task": "model-a", "owned-task": "model-new"}, merged
    assert broadcasts[0][0] == ("extension.config.internal_llm",), broadcasts


def test_update_extension_config_internal_llm_rejects_unowned_task_as_403() -> None:
    """An assignment for a task this extension does NOT own is a typed
    403 rejection — and exercises `_rejected`'s HTTPException branch."""
    record = {"id": "ext-1"}

    with _patch("extension_store", "get_extension", lambda eid: record), \
            _patch("extension_store", "extension_internal_llm_tasks", lambda _r: ["owned-task"]), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        port = build_system_command_port()
        result = _run(port.update_extension_config(
            "ext-1", "internal_llm", {"assignments": {"not-owned": "model"}},
        ))
    assert not result.accepted
    assert result.code == "403", result


def test_update_extension_config_internal_llm_not_installed_is_rejected() -> None:
    """A missing extension record raises ExtensionError -> `_rejected`'s
    non-HTTPException branch (code="invalid")."""
    import extension_store

    with _patch("extension_store", "get_extension", lambda eid: None), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        port = build_system_command_port()
        result = _run(port.update_extension_config("ext-1", "internal_llm", {"assignments": {}}))
    assert not result.accepted
    assert result.code == "invalid", result


def test_update_extension_config_unknown_section_is_rejected() -> None:
    port = build_system_command_port()
    result = _run(port.update_extension_config("ext-1", "no-such-section", {}))
    assert not result.accepted
    assert result.code == "unknown_section", result


# ---------------------------------------------------------------------------
# install / update / uninstall / enable-disable extensions
# ---------------------------------------------------------------------------

def test_install_extension_routes_each_source_shape() -> None:
    """marketplace_metadata_url / artifact_url / repo fallback each reach a
    distinct install function, then broadcast the catalog topics."""
    calls: list = []
    broadcasts: list = []

    def _factory(name):
        def _f(*a, **k):
            calls.append((name, a, k))
        return _f

    with _patch("extension_store", "install_from_marketplace_metadata", _factory("marketplace")), \
            _patch("extension_store", "install_from_artifact", _factory("artifact")), \
            _patch("extension_store", "install_from_repo", _factory("repo")), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder(broadcasts)):
        port = build_system_command_port()

        r1 = _run(port.install_extension("ext-1", {"marketplace_metadata_url": "https://m"}))
        r2 = _run(port.install_extension("ext-1", {"artifact_url": "https://a", "artifact_sha256": "deadbeef"}))
        r3 = _run(port.install_extension("ext-1", {"repo_url": "https://r", "extension_path": "p"}))

    assert r1.accepted and r2.accepted and r3.accepted, (r1, r2, r3)
    names = [c[0] for c in calls]
    assert names == ["marketplace", "artifact", "repo"], names
    assert broadcasts, "catalog broadcast not fired"


def test_install_extension_extension_error_is_rejected() -> None:
    import extension_store

    def _boom(*a, **k):
        raise extension_store.ExtensionError("nope")

    with _patch("extension_store", "install_from_repo", _boom), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        port = build_system_command_port()
        result = _run(port.install_extension("ext-1", {"repo_url": "https://r"}))
    assert not result.accepted
    assert result.code == "invalid", result


def test_update_extension_broadcasts_only_when_changed() -> None:
    """`updated: True` fans out catalog topics; `updated: False` skips them
    but still notifies the updates channel."""
    for updated, expect_catalog in [(True, True), (False, False)]:
        catalogs: list = []
        updates: list = []
        with _patch("extension_store", "apply_extension_update", lambda eid: {"updated": updated}), \
                _patch("extension_api", "_broadcast_extension_changed", _async_recorder(catalogs)), \
                _patch("extension_api", "_broadcast_extension_updates_changed", _async_recorder(updates)):
            port = build_system_command_port()
            result = _run(port.update_extension("ext-1"))
        assert result.accepted, result
        assert bool(catalogs) is expect_catalog, (updated, catalogs)
        assert updates, (updated, "updates channel not notified")


def test_update_extension_extension_error_is_rejected() -> None:
    import extension_store

    with _patch("extension_store", "apply_extension_update",
                lambda eid: (_ for _ in ()).throw(extension_store.ExtensionError("boom"))), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])), \
            _patch("extension_api", "_broadcast_extension_updates_changed", _async_recorder([])):
        port = build_system_command_port()
        result = _run(port.update_extension("ext-1"))
    assert not result.accepted
    assert result.code == "invalid", result


def test_uninstall_extension_calls_store_and_broadcasts() -> None:
    uninstalls: list = []
    broadcasts: list = []
    with _patch("extension_store", "uninstall", _recorder(uninstalls)), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder(broadcasts)):
        port = build_system_command_port()
        result = _run(port.uninstall_extension("ext-1"))
    assert result.accepted, result
    assert uninstalls[0][0] == ("ext-1",), uninstalls
    assert broadcasts[0][1].get("extension_id") == "ext-1", broadcasts


def test_uninstall_extension_extension_error_is_rejected() -> None:
    import extension_store

    with _patch("extension_store", "uninstall",
                lambda eid: (_ for _ in ()).throw(extension_store.ExtensionError("boom"))), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        port = build_system_command_port()
        result = _run(port.uninstall_extension("ext-1"))
    assert not result.accepted
    assert result.code == "invalid", result


def test_enable_and_disable_route_through_set_enabled() -> None:
    for method, enabled in [("enable_extension", True), ("disable_extension", False)]:
        sets: list = []
        broadcasts: list = []
        with _patch("extension_store", "set_enabled", _recorder(sets)), \
                _patch("extension_api", "_broadcast_extension_changed", _async_recorder(broadcasts)):
            port = build_system_command_port()
            result = _run(getattr(port, method)("ext-1"))
        assert result.accepted, (method, result)
        assert sets[0][0] == ("ext-1", enabled), (method, sets[0][0])
        assert broadcasts, (method, "broadcast not fired")


def test_enable_extension_consent_required_is_rejected() -> None:
    """`ExtensionConsentRequired` is caught alongside ExtensionError in
    `_set_enabled` and routed through `_rejected` (covers the multi-except)."""
    import extension_store

    def _boom(eid, enabled):
        raise extension_store.ExtensionConsentRequired("need consent")

    with _patch("extension_store", "set_enabled", _boom), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        port = build_system_command_port()
        result = _run(port.enable_extension("ext-1"))
    assert not result.accepted
    assert result.code == "invalid", result


# ---------------------------------------------------------------------------
# marketplace intent / device
# ---------------------------------------------------------------------------

class _FakeBridge:
    def __init__(self):
        self.calls: list = []
        self.raise_on: str | None = None

    async def approve(self, intent_id):
        self.calls.append(("approve", intent_id))
        if self.raise_on == "approve":
            raise ValueError("bad intent")

    async def reject(self, intent_id):
        self.calls.append(("reject", intent_id))

    async def revoke(self, device_ref):
        self.calls.append(("revoke", device_ref))
        if self.raise_on == "revoke":
            raise ValueError("bad device")


def test_decide_marketplace_intent_routes_approve_and_reject() -> None:
    for decision, expect in [("approve", "approve"), ("deny", "reject")]:
        bridge = _FakeBridge()
        with _patch("marketplace_bridge", "bridge", bridge):
            port = build_system_command_port()
            result = _run(port.decide_marketplace_intent("intent-1", decision))
        assert result.accepted, (decision, result)
        assert bridge.calls[0][0] == expect, (decision, bridge.calls)


def test_decide_marketplace_intent_value_error_is_rejected() -> None:
    bridge = _FakeBridge()
    bridge.raise_on = "approve"
    with _patch("marketplace_bridge", "bridge", bridge):
        port = build_system_command_port()
        result = _run(port.decide_marketplace_intent("intent-1", "approve"))
    assert not result.accepted
    assert result.code == "invalid", result


def test_revoke_marketplace_device_routes_to_bridge_and_handles_error() -> None:
    bridge = _FakeBridge()
    with _patch("marketplace_bridge", "bridge", bridge):
        port = build_system_command_port()
        ok = _run(port.revoke_marketplace_device("dev-1"))
        assert ok.accepted, ok
        assert bridge.calls[0] == ("revoke", "dev-1")

        bridge.raise_on = "revoke"
        bad = _run(port.revoke_marketplace_device("dev-1"))
    assert not bad.accepted
    assert bad.code == "invalid", bad


# ---------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------

def test_create_schedule_success_without_coordinator() -> None:
    creates: list = []
    with _patch("stores.schedule_store", "create", _recorder(creates)), \
            _patch("orchestrator", "get_active_coordinator", lambda: None):
        port = build_system_command_port()
        result = _run(port.create_schedule(
            "sess-1", "do thing", {"kind": "once", "fire_at": "2026-01-01T00:00:00Z"},
        ))
    assert result.accepted, result
    assert creates[0][1].get("app_session_id") == "sess-1", creates


def test_create_schedule_value_error_is_rejected() -> None:
    def _boom(**kwargs):
        raise ValueError("bad cadence")

    with _patch("stores.schedule_store", "create", _boom), \
            _patch("orchestrator", "get_active_coordinator", lambda: None):
        port = build_system_command_port()
        result = _run(port.create_schedule("sess-1", "p", {"kind": "bogus"}))
    assert not result.accepted
    assert result.code == "invalid", result


def test_create_schedule_broadcasts_when_coordinator_active() -> None:
    broadcasts: list = []

    class _Coord:
        pass

    with _patch("stores.schedule_store", "create", lambda **k: None), \
            _patch("orchestrator", "get_active_coordinator", lambda: _Coord()), \
            _patch("scheduler", "broadcast_schedules", _async_recorder(broadcasts)):
        port = build_system_command_port()
        result = _run(port.create_schedule("sess-1", "p", {"kind": "once"}))
    assert result.accepted, result
    assert broadcasts[0][0][1] == "sess-1", broadcasts


def test_delete_schedule_success_broadcasts_to_session() -> None:
    broadcasts: list = []
    with _patch("stores.schedule_store", "delete", lambda sid: {"app_session_id": "sess-1"}), \
            _patch("orchestrator", "get_active_coordinator", lambda: object()), \
            _patch("scheduler", "broadcast_schedules", _async_recorder(broadcasts)):
        port = build_system_command_port()
        result = _run(port.delete_schedule("sched-1"))
    assert result.accepted, result
    assert broadcasts[0][0][1] == "sess-1", broadcasts


def test_delete_schedule_unknown_id_is_404() -> None:
    with _patch("stores.schedule_store", "delete", lambda sid: None), \
            _patch("orchestrator", "get_active_coordinator", lambda: None):
        port = build_system_command_port()
        result = _run(port.delete_schedule("no-such-schedule"))
    assert not result.accepted
    assert result.code == "404", result


# ---------------------------------------------------------------------------
# remove_node
# ---------------------------------------------------------------------------

def test_remove_node_skips_topology_when_registry_holds_it() -> None:
    """Registry removal (truthy) short-circuits topology.remove_node but
    still tears down credentials + node_store."""
    topo_calls: list = []
    cred_calls: list = []

    async def _forget(node_id):
        cred_calls.append(("forget", node_id))

    with _patch("node_registry_store", "remove", lambda nid: True), \
            _patch("topology", "remove_node", _recorder(topo_calls)), \
            _patch("node_provider_credential_sync", "remove_node", _recorder([])), \
            _patch("node_store", "forget", _forget):
        port = build_system_command_port()
        result = _run(port.remove_node("node-1"))
    assert result.accepted, result
    assert not topo_calls, topo_calls
    assert any(c[0] == "forget" for c in cred_calls), cred_calls


def test_remove_node_topology_removes_when_registry_misses() -> None:
    removed: list = []

    async def _forget(node_id):
        removed.append(node_id)

    with _patch("node_registry_store", "remove", lambda nid: False), \
            _patch("topology", "remove_node", lambda nid: True), \
            _patch("node_provider_credential_sync", "remove_node", lambda nid: None), \
            _patch("node_store", "forget", _forget):
        port = build_system_command_port()
        result = _run(port.remove_node("node-1"))
    assert result.accepted, result
    assert removed == ["node-1"], removed


def test_remove_node_unknown_is_404() -> None:
    with _patch("node_registry_store", "remove", lambda nid: False), \
            _patch("topology", "remove_node", lambda nid: False), \
            _patch("node_provider_credential_sync", "remove_node", lambda nid: None):
        async def _forget(node_id):
            raise AssertionError("forget should not run for unknown node")
        with _patch("node_store", "forget", _forget):
            port = build_system_command_port()
            result = _run(port.remove_node("node-1"))
    assert not result.accepted
    assert result.code == "404", result


def test_remove_node_topology_error_is_rejected() -> None:
    import topology

    def _boom(nid):
        raise topology.TopologyError("unreachable")

    with _patch("node_registry_store", "remove", lambda nid: False), \
            _patch("topology", "remove_node", _boom), \
            _patch("node_provider_credential_sync", "remove_node", lambda nid: None):
        port = build_system_command_port()
        result = _run(port.remove_node("node-1"))
    assert not result.accepted
    assert result.code == "invalid", result


# ---------------------------------------------------------------------------
# resolve_node_registration
# ---------------------------------------------------------------------------

def test_resolve_node_registration_approved_returns_rec_and_reason() -> None:
    async def _approve(nid):
        return ({"node_id": nid}, "ok")

    with _patch("node_link", "approve_registration", _approve):
        port = build_system_command_port()
        result = _run(port.resolve_node_registration("node-1", "approved"))
    assert result.accepted, result


def test_resolve_node_registration_missing_is_404_expired_is_410() -> None:
    async def _approve(nid):
        return (None, "missing")

    async def _deny(nid):
        return (None, "expired")

    with _patch("node_link", "approve_registration", _approve), \
            _patch("node_link", "deny_registration", _deny):
        port = build_system_command_port()
        missing = _run(port.resolve_node_registration("node-1", "approved"))
        expired = _run(port.resolve_node_registration("node-1", "denied"))
    assert missing.code == "404", missing
    assert expired.code == "410", expired


def test_save_harness_profile_field_error_is_typed_invalid_field() -> None:
    """`harness_fields.HarnessFieldError` from `apply_field_writes` ->
    `code="invalid_field"` (covers line 176)."""
    import harness_fields
    import harness_field_writer

    def _boom(*a, **k):
        raise harness_fields.HarnessFieldError("bad field")

    port = build_system_command_port()
    with _patch("harness_field_writer", "apply_field_writes", _boom), \
            _patch("harness_profiles_api", "_broadcast_harness_profiles_changed", _async_recorder([])), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        result = _run(port.save_harness_profile(
            "some-profile", {}, "rev", ({"path": ["x"], "value": 1},),
        ))
    assert not result.accepted
    assert result.code == "invalid_field", result


def test_save_harness_profile_generic_profile_error_is_stale_revision() -> None:
    """A bare `HarnessProfileError` from the field-write path (the DEFAULT
    guard above already pre-empted, so only the optimistic-concurrency
    re-check remains) -> `code="stale_revision"` (covers lines 180-181)."""
    import harness_field_writer
    import harness_profile_store

    def _boom(*a, **k):
        raise harness_profile_store.HarnessProfileError("conflict")

    port = build_system_command_port()
    with _patch("harness_field_writer", "apply_field_writes", _boom), \
            _patch("harness_profiles_api", "_broadcast_harness_profiles_changed", _async_recorder([])), \
            _patch("extension_api", "_broadcast_extension_changed", _async_recorder([])):
        result = _run(port.save_harness_profile(
            "some-profile", {}, "rev", ({"path": ["x"], "value": 1},),
        ))
    assert not result.accepted
    assert result.code == "stale_revision", result


def test_delete_harness_profile_store_returns_falsy_is_404() -> None:
    """`delete_profile` returning a falsy value (no row matched the id) ->
    `code="404"` (covers line 209)."""
    port = build_system_command_port()
    with _patch("harness_profile_store", "delete_profile", lambda *a, **k: None), \
            _patch("harness_profiles_api", "_broadcast_harness_profiles_changed", _async_recorder([])):
        result = _run(port.delete_harness_profile("some-profile", "rev"))
    assert not result.accepted
    assert result.code == "404", result


def test_delete_schedule_success_without_coordinator_skips_broadcast() -> None:
    """A successful delete with no active coordinator skips the broadcast
    (covers the `coordinator is None` branch 355->357 on the success path)."""
    broadcasts: list = []
    with _patch("stores.schedule_store", "delete", lambda sid: {"app_session_id": "sess-1"}), \
            _patch("orchestrator", "get_active_coordinator", lambda: None), \
            _patch("scheduler", "broadcast_schedules", _async_recorder(broadcasts)):
        port = build_system_command_port()
        result = _run(port.delete_schedule("sched-1"))
    assert result.accepted, result
    assert not broadcasts, broadcasts


class _RaisingBus:
    async def publish(self, event):
        raise RuntimeError("bus down")


def test_set_installation_capability_profile_error_is_rejected() -> None:
    """`installation_profile.InstallationProfileError` -> `_rejected`
    (covers lines 385-386)."""
    _test_installation.activate(Path(paths.ba_home()))
    import installation_profile

    def _boom(*a, **k):
        raise installation_profile.InstallationProfileError("blocked")

    with _patch("installation_profile", "set_capability_enabled", _boom):
        port = build_system_command_port()
        result = _run(port.set_installation_capability("mobile", True, False))
    assert not result.accepted
    assert result.code == "invalid", result


def test_set_installation_capability_broadcasts_globally_when_coordinator_active() -> None:
    """An active coordinator receives a `broadcast_global` with the new
    capability state (covers line 389)."""
    _test_installation.activate(Path(paths.ba_home()))

    class _Coord:
        def __init__(self):
            self.global_events: list = []

        async def broadcast_global(self, event_type, state):
            self.global_events.append((event_type, state))

    coord = _Coord()
    with _patch("orchestrator", "get_active_coordinator", lambda: coord):
        port = build_system_command_port()
        result = _run(port.set_installation_capability("mobile", True, False))
    assert result.accepted, result
    assert coord.global_events, "coordinator.broadcast_global not called"
    assert coord.global_events[0][0] == "installation_capabilities_changed", coord.global_events


def test_set_installation_capability_survives_bus_publish_failure() -> None:
    """A failing `bus.publish` is caught and logged, not raised — the
    capability change still reports accepted (covers lines 405-406)."""
    _test_installation.activate(Path(paths.ba_home()))
    with _patch("event_bus", "bus", _RaisingBus()):
        port = build_system_command_port()
        result = _run(port.set_installation_capability("mobile", True, False))
    assert result.accepted, result


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
    captured: dict[str, object] = {}

    async def _fake_authorize_and_sync(node_id: str, provider_ids: object) -> dict:
        captured["node_id"] = node_id
        captured["provider_ids"] = provider_ids
        return {"node_id": node_id, "provider_credentials": []}

    with _patch("node_provider_credential_sync", "authorize_and_sync", _fake_authorize_and_sync):
        port = build_system_command_port()
        result = _run(port.sync_node_providers("worker-1", True, ("provider-1", "provider-2")))

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
    test_set_default_harness_profile_is_unsupported,
    test_update_extension_config_simple_sections_dispatch_and_broadcast,
    test_update_extension_config_internal_llm_merges_owned_assignments,
    test_update_extension_config_internal_llm_rejects_unowned_task_as_403,
    test_update_extension_config_internal_llm_not_installed_is_rejected,
    test_update_extension_config_unknown_section_is_rejected,
    test_install_extension_routes_each_source_shape,
    test_install_extension_extension_error_is_rejected,
    test_update_extension_broadcasts_only_when_changed,
    test_update_extension_extension_error_is_rejected,
    test_uninstall_extension_calls_store_and_broadcasts,
    test_uninstall_extension_extension_error_is_rejected,
    test_enable_and_disable_route_through_set_enabled,
    test_enable_extension_consent_required_is_rejected,
    test_decide_marketplace_intent_routes_approve_and_reject,
    test_decide_marketplace_intent_value_error_is_rejected,
    test_revoke_marketplace_device_routes_to_bridge_and_handles_error,
    test_create_schedule_success_without_coordinator,
    test_create_schedule_value_error_is_rejected,
    test_create_schedule_broadcasts_when_coordinator_active,
    test_delete_schedule_success_broadcasts_to_session,
    test_delete_schedule_unknown_id_is_404,
    test_remove_node_skips_topology_when_registry_holds_it,
    test_remove_node_topology_removes_when_registry_misses,
    test_remove_node_unknown_is_404,
    test_remove_node_topology_error_is_rejected,
    test_resolve_node_registration_approved_returns_rec_and_reason,
    test_resolve_node_registration_missing_is_404_expired_is_410,
    test_save_harness_profile_field_error_is_typed_invalid_field,
    test_save_harness_profile_generic_profile_error_is_stale_revision,
    test_delete_harness_profile_store_returns_falsy_is_404,
    test_delete_schedule_success_without_coordinator_skips_broadcast,
    test_set_installation_capability_profile_error_is_rejected,
    test_set_installation_capability_broadcasts_globally_when_coordinator_active,
    test_set_installation_capability_survives_bus_publish_failure,
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
