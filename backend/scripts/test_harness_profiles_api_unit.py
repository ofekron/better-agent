"""Unit owner for harness_profiles_api.py.

The module is a FastAPI router mounted at runtime by app_composition / main
and never imported by any pytest test (only referenced as static text in a
couple of guard scans) -> 0% unit coverage. This owner drives every helper
and route handler directly: pure response shapers with plain dicts, and
async handlers via asyncio.run with store/resolver/config collaborators
patched at the module boundary. No source edit, no pragma.
"""

from __future__ import annotations

import asyncio
import sys
import types as pytypes

import pytest
from fastapi import HTTPException

import harness_profiles_api as api


# --- fakes ------------------------------------------------------------------

def _async_recorder(store: list | None = None):
    calls: list = store if store is not None else []

    async def _fn(*args, **kwargs):
        calls.append((args, kwargs))

    return _fn, calls


@pytest.fixture(autouse=True)
def _reset_broadcast():
    api._broadcast_global = None
    yield
    api._broadcast_global = None


def _run(coro):
    return asyncio.run(coro)


def _patch_fields_scope_safe(monkeypatch):
    """Make _field_writes_change_global complete without raising so the test
    reaches apply_field_writes (where each error test raises its target
    exception into the correct except clause)."""
    monkeypatch.setattr(api.harness_fields, "GROUP_PROFILE_META", "profile_meta")
    monkeypatch.setattr(api.harness_fields, "scope_for", lambda path: "profile")


# ---------------------------------------------------------------------------
# configure / _require_configured
# ---------------------------------------------------------------------------

def test_require_configured_raises_when_not_bound():
    with pytest.raises(HTTPException) as ei:
        api._require_configured()
    assert ei.value.status_code == 503


def test_configure_binds_broadcast():
    fn, _ = _async_recorder()
    api.configure(fn)
    assert api._require_configured() is fn


# ---------------------------------------------------------------------------
# pure response shapers
# ---------------------------------------------------------------------------

def test_resolved_override_response_passthrough():
    entry = {"resolved": [1], "override": {"a": 1}, "extra": "ignored"}
    assert api._resolved_override_response(entry) == {"resolved": [1], "override": {"a": 1}}


def test_profile_instance_fields_response_skips_none_and_shapes_overlays():
    fields = {
        "mcp_servers": {"resolved": "r", "override": "o"},
        "skills": None,
        "setting_overlays": {
            "ov1": {"resolved": 1, "override": 2},
            "ov2": {"resolved": 3, "override": 4},
        },
    }
    out = api._profile_instance_fields_response(fields)
    assert out["mcp_servers"] == {"resolved": "r", "override": "o"}
    assert "skills" not in out
    assert out["setting_overlays"] == {
        "ov1": {"resolved": 1, "override": 2},
        "ov2": {"resolved": 3, "override": 4},
    }


def test_profile_instance_fields_response_empty():
    assert api._profile_instance_fields_response({}) == {}


def test_profile_response_from_resolved_full():
    resolved = {
        "id": "p1",
        "name": "P",
        "description": "d",
        "revision": "rev1",
        "created_at": "c",
        "updated_at": "u",
        "disabled_builtin_tools": {"resolved": [], "override": None},
        "disabled_builtin_extensions": {"resolved": [], "override": None},
        "extension_instances": {
            "ext-a": {"mcp_servers": {"resolved": 1, "override": 2}},
        },
        "instruction_sources": {"core": {"resolved": "x", "override": "y"}},
        "mcp_overrides": {"k": "v"},
        "skill_overrides": {"sk": "sv"},
        "native_harness_overrides": {"n": "nv"},
        "provider_run_config_overlay": {"model": "m"},
    }
    out = api._profile_response_from_resolved(resolved)
    assert out["id"] == "p1"
    assert out["name"] == "P"
    assert out["revision"] == "rev1"
    assert out["fields"]["disabled_builtin_tools"] == {"resolved": [], "override": None}
    assert out["fields"]["extension_instances"]["ext-a"]["mcp_servers"] == {"resolved": 1, "override": 2}
    assert out["fields"]["instruction_sources"]["core"] == {"resolved": "x", "override": "y"}
    assert out["mcp_overrides"] == {"k": "v"}
    assert out["skill_overrides"] == {"sk": "sv"}
    assert out["native_harness_overrides"] == {"n": "nv"}
    assert out["provider_run_config_overlay"] == {"model": "m"}


def test_with_profile_meta_attaches_and_defaults():
    resp = {"id": "p"}
    out = api._with_profile_meta(dict(resp), None)
    assert out["base_profile_id"] is None
    assert out["read_only"] is False
    assert out["source"] == ""
    assert out["extension_id"] == ""


def test_with_profile_meta_from_stored():
    stored = {
        "base_profile_id": "base", "base_profile_revision": "br",
        "default_runtime_profile_id": "rt", "default_model": "m",
        "default_reasoning_effort": "high", "provisioning_prompt": "pp",
        "read_only": True, "source": "src", "extension_id": "ext",
    }
    out = api._with_profile_meta({}, stored)
    assert out["base_profile_id"] == "base"
    assert out["default_model"] == "m"
    assert out["read_only"] is True
    assert out["source"] == "src"
    assert out["extension_id"] == "ext"


def test_profile_response_uses_resolver(monkeypatch):
    resolved = {"id": "p1", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    out = api._profile_response({"id": "p1", "revision": "r"})
    assert out["id"] == "p1"


def test_profile_summary_response_default():
    out = api._profile_summary_response(None)
    assert out["id"] == "default"
    assert out["name"] == "Default"
    assert out["read_only"] is False


def test_profile_summary_response_stored():
    out = api._profile_summary_response({"id": "p1", "name": "P", "revision": "r", "description": "d"})
    assert out["id"] == "p1"
    assert out["name"] == "P"
    assert out["revision"] == "r"
    assert out["description"] == "d"


def test_default_profile_response(monkeypatch):
    resolved = {"id": "x", "name": "ignored", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    out = api._default_profile_response()
    assert out["id"] == "default"
    assert out["name"] == "Default"
    assert out["base_profile_id"] is None  # stored=None


def test_profile_field_write_response_default_when_none(monkeypatch):
    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", lambda pid, rev, writes: None)
    resolved = {"id": "default", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    out = api._profile_field_write_response("default", None, [])
    assert out["id"] == "default"


def test_profile_field_write_response_stored(monkeypatch):
    stored = {"id": "p1", "revision": "r"}
    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", lambda pid, rev, writes: stored)
    resolved = {"id": "p1", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    out = api._profile_field_write_response("p1", "r", [])
    assert out["id"] == "p1"


# ---------------------------------------------------------------------------
# _field_writes_change_global
# ---------------------------------------------------------------------------

def test_field_writes_change_global_default_profile():
    assert api._field_writes_change_global(api.harness_profile_store.DEFAULT_PROFILE_ID, []) is True


def test_field_writes_change_global_meta_only(monkeypatch):
    monkeypatch.setattr(api.harness_fields, "GROUP_PROFILE_META", "profile_meta")
    monkeypatch.setattr(api.harness_fields, "scope_for", lambda path: "profile")
    writes = [{"path": ["profile_meta", "name"]}]
    assert api._field_writes_change_global("p1", writes) is False


def test_field_writes_change_global_scope(monkeypatch):
    monkeypatch.setattr(api.harness_fields, "GROUP_PROFILE_META", "profile_meta")
    monkeypatch.setattr(api.harness_fields, "scope_for", lambda path: api.harness_fields.SCOPE_GLOBAL)
    writes = [{"path": ["ext", "tool"]}]
    assert api._field_writes_change_global("p1", writes) is True


def test_field_writes_change_global_empty_path_skipped(monkeypatch):
    monkeypatch.setattr(api.harness_fields, "GROUP_PROFILE_META", "profile_meta")
    monkeypatch.setattr(api.harness_fields, "scope_for", lambda path: "profile")
    writes = [{"path": []}, {"path": None}]
    assert api._field_writes_change_global("p1", writes) is False


# ---------------------------------------------------------------------------
# harness_profile_selection
# ---------------------------------------------------------------------------

def test_harness_profile_selection_blank():
    assert api.harness_profile_selection({}) == ""
    assert api.harness_profile_selection(None) == ""
    assert api.harness_profile_selection({"harness_profile_id": "   "}) == ""


def test_harness_profile_selection_store_error_then_missing(monkeypatch):
    def raise_err(pid):
        raise api.harness_profile_store.HarnessProfileError("boom")

    monkeypatch.setattr(api.harness_profile_store, "get_profile", raise_err)
    with pytest.raises(HTTPException) as ei:
        api.harness_profile_selection({"harness_profile_id": "ghost"})
    assert ei.value.status_code == 404


def test_harness_profile_selection_not_found(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: None)
    with pytest.raises(HTTPException) as ei:
        api.harness_profile_selection({"harness_profile_id": "ghost"})
    assert ei.value.status_code == 404


def test_harness_profile_selection_resolution_error(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: {"id": pid})

    def raise_res(*a, **k):
        raise api.harness_profile_resolver.HarnessProfileResolutionError("bad")

    monkeypatch.setattr(api.harness_profile_resolver, "resolve_for_session", raise_res)
    with pytest.raises(HTTPException) as ei:
        api.harness_profile_selection({"harness_profile_id": "p1"})
    assert ei.value.status_code == 400


def test_harness_profile_selection_ok(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: {"id": pid})
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_for_session", lambda *a, **k: None)
    assert api.harness_profile_selection({"harness_profile_id": "p1"}) == "p1"


# ---------------------------------------------------------------------------
# _broadcast_harness_profiles_changed
# ---------------------------------------------------------------------------

def _fake_node_module():
    mod = pytypes.ModuleType("node_config_sync")
    mod.notify_changed = lambda *a, **k: None
    return mod


def test_broadcast_harness_profiles_changed_ok(monkeypatch):
    invalidated = []
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: invalidated.append(True))
    bcast, calls = _async_recorder()
    api.configure(bcast)
    fake_node = _fake_node_module()
    notify_calls = []
    fake_node.notify_changed = lambda *a: notify_calls.append(a)
    monkeypatch.setitem(sys.modules, "node_config_sync", fake_node)
    _run(api._broadcast_harness_profiles_changed({"x": 1}))
    assert invalidated == [True]
    assert calls and calls[0][0] == ("harness_profiles_changed", {"x": 1})
    assert notify_calls == [("harness",)]


def test_broadcast_harness_profiles_changed_default_payload(monkeypatch):
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: None)
    bcast, calls = _async_recorder()
    api.configure(bcast)
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    _run(api._broadcast_harness_profiles_changed(None))
    assert calls[0][0] == ("harness_profiles_changed", {})


def test_broadcast_harness_profiles_changed_broadcast_raises_swallowed(monkeypatch):
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: None)

    async def boom(*a, **k):
        raise RuntimeError("broadcast down")

    api.configure(boom)
    fake_node = _fake_node_module()
    notify_calls = []
    fake_node.notify_changed = lambda *a: notify_calls.append(a)
    monkeypatch.setitem(sys.modules, "node_config_sync", fake_node)
    _run(api._broadcast_harness_profiles_changed(None))  # must not raise
    assert notify_calls == [("harness",)]  # node sync still attempted


def test_broadcast_harness_profiles_changed_not_configured_swallowed(monkeypatch):
    # _require_configured raises 503 -> caught by the broad except, logged.
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: None)
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    _run(api._broadcast_harness_profiles_changed(None))  # must not raise


def test_broadcast_harness_profiles_changed_node_sync_raises_swallowed(monkeypatch):
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: None)
    bcast, _ = _async_recorder()
    api.configure(bcast)
    fake_node = _fake_node_module()
    fake_node.notify_changed = lambda *a: (_ for _ in ()).throw(RuntimeError("node down"))
    monkeypatch.setitem(sys.modules, "node_config_sync", fake_node)
    _run(api._broadcast_harness_profiles_changed(None))  # must not raise


# ---------------------------------------------------------------------------
# route handlers
# ---------------------------------------------------------------------------

def test_list_harness_profiles(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "list_profiles", lambda: [{"id": "p1", "name": "P", "revision": "r"}])
    out = _run(api.list_harness_profiles())
    assert out["profiles"][0]["id"] == "default"  # synthesized default first
    assert out["profiles"][1]["id"] == "p1"


def test_get_harness_profile_descriptor_ok(monkeypatch):
    monkeypatch.setattr(api.harness_fields, "descriptor", lambda: {"groups": []})
    assert _run(api.get_harness_profile_descriptor()) == {"groups": []}


def test_get_harness_profile_descriptor_error(monkeypatch):
    def raise_err():
        raise api.harness_fields.HarnessFieldError("bad")

    monkeypatch.setattr(api.harness_fields, "descriptor", raise_err)
    with pytest.raises(HTTPException) as ei:
        _run(api.get_harness_profile_descriptor())
    assert ei.value.status_code == 400


def _patch_writes_response(monkeypatch, stored):
    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", lambda pid, rev, writes: stored)
    resolved = {"id": stored["id"], "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)


def test_patch_harness_profile_fields_body_not_object():
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_fields("p1", "not-a-dict"))
    assert ei.value.status_code == 400


def test_patch_harness_profile_fields_writes_invalid():
    for body in [{"writes": "x"}, {"writes": []}, {"writes": [{}]}]:
        with pytest.raises(HTTPException) as ei:
            _run(api.patch_harness_profile_fields("p1", dict(body)))
        assert ei.value.status_code == 400
    # path not a list / empty
    with pytest.raises(HTTPException):
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": "nope"}]}))
    with pytest.raises(HTTPException):
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": []}]}))


def test_patch_harness_profile_fields_ok_global(monkeypatch):
    stored = {"id": "p1", "revision": "r"}
    _patch_writes_response(monkeypatch, stored)
    monkeypatch.setattr(api.harness_fields, "GROUP_PROFILE_META", "profile_meta")
    monkeypatch.setattr(api.harness_fields, "scope_for", lambda path: api.harness_fields.SCOPE_GLOBAL)
    ext_calls = []
    monkeypatch.setattr(api.extension_api, "_broadcast_extension_changed", _async_recorder(ext_calls)[0])
    api.configure(_async_recorder()[0])
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    out = _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["a"]}]}))
    assert out["id"] == "p1"
    assert ext_calls  # global change -> extension broadcast


def test_patch_harness_profile_fields_ok_non_global(monkeypatch):
    stored = {"id": "p1", "revision": "r"}
    _patch_writes_response(monkeypatch, stored)
    monkeypatch.setattr(api.harness_fields, "GROUP_PROFILE_META", "profile_meta")
    monkeypatch.setattr(api.harness_fields, "scope_for", lambda path: "profile")
    ext_calls = []
    monkeypatch.setattr(api.extension_api, "_broadcast_extension_changed", _async_recorder(ext_calls)[0])
    api.configure(_async_recorder()[0])
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    out = _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["profile_meta", "x"]}]}))
    assert out["id"] == "p1"
    assert not ext_calls  # non-global -> no extension broadcast


def test_patch_harness_profile_fields_not_found(monkeypatch):
    def raise_nf(pid, rev, writes):
        raise api.harness_profile_store.HarnessProfileNotFoundError("nope")

    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", raise_nf)
    _patch_fields_scope_safe(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["a"]}]}))
    assert ei.value.status_code == 404


def test_patch_harness_profile_fields_store_error(monkeypatch):
    def raise_err(pid, rev, writes):
        raise api.harness_profile_store.HarnessProfileError("bad")

    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", raise_err)
    _patch_fields_scope_safe(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["a"]}]}))
    assert ei.value.status_code == 400


def test_patch_harness_profile_fields_field_error(monkeypatch):
    def raise_err(pid, rev, writes):
        raise api.harness_fields.HarnessFieldError("bad")

    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", raise_err)
    _patch_fields_scope_safe(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["a"]}]}))
    assert ei.value.status_code == 400


def test_patch_harness_profile_fields_extension_error(monkeypatch):
    def raise_err(pid, rev, writes):
        raise api.extension_store.ExtensionError("bad")

    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", raise_err)
    _patch_fields_scope_safe(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["a"]}]}))
    assert ei.value.status_code == 400


def test_patch_harness_profile_fields_resolution_error(monkeypatch):
    def raise_err(pid, rev, writes):
        raise api.harness_profile_resolver.HarnessProfileResolutionError("bad")

    monkeypatch.setattr(api.harness_field_writer, "apply_field_writes", raise_err)
    _patch_fields_scope_safe(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_fields("p1", {"writes": [{"path": ["a"]}]}))
    assert ei.value.status_code == 400


def test_get_harness_profile_default(monkeypatch):
    resolved = {"id": "default", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    out = _run(api.get_harness_profile(api.harness_profile_store.DEFAULT_PROFILE_ID))
    assert out["id"] == "default"


def test_get_harness_profile_store_error(monkeypatch):
    def raise_err(pid):
        raise api.harness_profile_store.HarnessProfileError("bad")

    monkeypatch.setattr(api.harness_profile_store, "get_profile", raise_err)
    with pytest.raises(HTTPException) as ei:
        _run(api.get_harness_profile("p1"))
    assert ei.value.status_code == 400


def test_get_harness_profile_not_found(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: None)
    with pytest.raises(HTTPException) as ei:
        _run(api.get_harness_profile("ghost"))
    assert ei.value.status_code == 404


def test_get_harness_profile_ok(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: {"id": pid, "revision": "r"})
    resolved = {"id": "p1", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    out = _run(api.get_harness_profile("p1"))
    assert out["id"] == "p1"


def test_create_harness_profile_body_not_object():
    with pytest.raises(HTTPException) as ei:
        _run(api.create_harness_profile("nope"))
    assert ei.value.status_code == 400


def test_create_harness_profile_store_error(monkeypatch):
    def raise_err(body):
        raise api.harness_profile_store.HarnessProfileError("bad")

    monkeypatch.setattr(api.harness_profile_store, "create_profile", raise_err)
    with pytest.raises(HTTPException) as ei:
        _run(api.create_harness_profile({}))
    assert ei.value.status_code == 400


def test_create_harness_profile_ok(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "create_profile", lambda body: {"id": "p1", "revision": "r"})
    resolved = {"id": "p1", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    api.configure(_async_recorder()[0])
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    out = _run(api.create_harness_profile({"name": "P"}))
    assert out["id"] == "p1"


def test_patch_harness_profile_overrides_body_not_object():
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_overrides("p1", "nope"))
    assert ei.value.status_code == 400


def test_patch_harness_profile_overrides_ops_not_list():
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_overrides("p1", {"ops": "x"}))
    assert ei.value.status_code == 400


def test_patch_harness_profile_overrides_not_found(monkeypatch):
    def raise_nf(pid, ops, rev):
        raise api.harness_profile_store.HarnessProfileNotFoundError("nope")

    monkeypatch.setattr(api.harness_profile_store, "apply_override_patch", raise_nf)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_overrides("p1", {"ops": []}))
    assert ei.value.status_code == 404


def test_patch_harness_profile_overrides_store_error(monkeypatch):
    def raise_err(pid, ops, rev):
        raise api.harness_profile_store.HarnessProfileError("bad")

    monkeypatch.setattr(api.harness_profile_store, "apply_override_patch", raise_err)
    with pytest.raises(HTTPException) as ei:
        _run(api.patch_harness_profile_overrides("p1", {"ops": []}))
    assert ei.value.status_code == 400


def test_patch_harness_profile_overrides_ok(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "apply_override_patch", lambda pid, ops, rev: {"id": pid, "revision": "r"})
    resolved = {"id": "p1", "disabled_builtin_tools": {"resolved": [], "override": None},
                "disabled_builtin_extensions": {"resolved": [], "override": None}}
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_profile", lambda *a, **k: resolved)
    api.configure(_async_recorder()[0])
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    out = _run(api.patch_harness_profile_overrides("p1", {"ops": [{"op": "add"}]}))
    assert out["id"] == "p1"


def test_delete_harness_profile_not_found(monkeypatch):
    def raise_nf(pid, rev):
        raise api.harness_profile_store.HarnessProfileNotFoundError("nope")

    monkeypatch.setattr(api.harness_profile_store, "delete_profile", raise_nf)
    with pytest.raises(HTTPException) as ei:
        _run(api.delete_harness_profile("p1"))
    assert ei.value.status_code == 404


def test_delete_harness_profile_store_error(monkeypatch):
    def raise_err(pid, rev):
        raise api.harness_profile_store.HarnessProfileError("bad")

    monkeypatch.setattr(api.harness_profile_store, "delete_profile", raise_err)
    with pytest.raises(HTTPException) as ei:
        _run(api.delete_harness_profile("p1"))
    assert ei.value.status_code == 400


def test_delete_harness_profile_not_deleted(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "delete_profile", lambda pid, rev: False)
    with pytest.raises(HTTPException) as ei:
        _run(api.delete_harness_profile("p1"))
    assert ei.value.status_code == 404


def test_delete_harness_profile_ok(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "delete_profile", lambda pid, rev: True)
    api.configure(_async_recorder()[0])
    monkeypatch.setitem(sys.modules, "node_config_sync", _fake_node_module())
    out = _run(api.delete_harness_profile("p1"))
    assert out == {"success": True}


def test_update_session_harness_profile_body_not_object():
    with pytest.raises(HTTPException) as ei:
        _run(api.update_session_harness_profile("s1", "nope"))
    assert ei.value.status_code == 400


def test_update_session_harness_profile_session_missing(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: {"id": pid})
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_for_session", lambda *a, **k: None)
    monkeypatch.setattr(api.session_manager, "set_harness_profile", lambda sid, pid: None)
    with pytest.raises(HTTPException) as ei:
        _run(api.update_session_harness_profile("s1", {"harness_profile_id": "p1"}))
    assert ei.value.status_code == 404


def test_update_session_harness_profile_ok(monkeypatch):
    monkeypatch.setattr(api.harness_profile_store, "get_profile", lambda pid: {"id": pid})
    monkeypatch.setattr(api.harness_profile_resolver, "resolve_for_session", lambda *a, **k: None)
    monkeypatch.setattr(api.session_manager, "set_harness_profile", lambda sid, pid: {"harness_profile_id": pid})
    out = _run(api.update_session_harness_profile("s1", {"harness_profile_id": "p1"}))
    assert out == {"id": "s1", "harness_profile_id": "p1"}


def test_update_session_harness_profile_blank_profile_ok(monkeypatch):
    # blank selection -> profile_id "" -> still sets on session
    monkeypatch.setattr(api.session_manager, "set_harness_profile", lambda sid, pid: {"harness_profile_id": pid})
    out = _run(api.update_session_harness_profile("s1", {}))
    assert out == {"id": "s1", "harness_profile_id": ""}


def test_get_global_disabled_builtin_tools(monkeypatch):
    monkeypatch.setattr(api.config_store, "get_disabled_builtin_tools", lambda: ["t1"])
    assert _run(api.get_global_disabled_builtin_tools()) == {"disabled_builtin_tools": ["t1"]}


def test_set_global_disabled_builtin_tools_body_not_object():
    with pytest.raises(HTTPException) as ei:
        _run(api.set_global_disabled_builtin_tools("nope"))
    assert ei.value.status_code == 400


def test_set_global_disabled_builtin_tools_tools_not_list():
    with pytest.raises(HTTPException) as ei:
        _run(api.set_global_disabled_builtin_tools({"disabled_builtin_tools": "x"}))
    assert ei.value.status_code == 400


def test_set_global_disabled_builtin_tools_ok(monkeypatch):
    monkeypatch.setattr(api.config_store, "set_disabled_builtin_tools", lambda tools: tools)
    invalidated = []
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: invalidated.append(True))
    ext_calls = []
    monkeypatch.setattr(api.extension_api, "_broadcast_extension_changed", _async_recorder(ext_calls)[0])
    out = _run(api.set_global_disabled_builtin_tools({"disabled_builtin_tools": ["t1"]}))
    assert out == {"disabled_builtin_tools": ["t1"]}
    assert invalidated == [True]
    assert ext_calls


def test_get_global_disabled_builtin_extensions(monkeypatch):
    monkeypatch.setattr(api.config_store, "get_disabled_builtin_extensions", lambda: ["e1"])
    assert _run(api.get_global_disabled_builtin_extensions()) == {"disabled_builtin_extensions": ["e1"]}


def test_set_global_disabled_builtin_extensions_body_not_object():
    with pytest.raises(HTTPException) as ei:
        _run(api.set_global_disabled_builtin_extensions("nope"))
    assert ei.value.status_code == 400


def test_set_global_disabled_builtin_extensions_ok(monkeypatch):
    monkeypatch.setattr(api, "api_disabled_builtin_extensions", lambda ids: ids)
    monkeypatch.setattr(api.config_store, "set_disabled_builtin_extensions", lambda ids: ids)
    invalidated = []
    monkeypatch.setattr(api.harness_profile_resolver, "invalidate_cache", lambda: invalidated.append(True))
    ext_calls = []
    monkeypatch.setattr(api.extension_api, "_broadcast_extension_changed", _async_recorder(ext_calls)[0])
    out = _run(api.set_global_disabled_builtin_extensions({"disabled_builtin_extensions": ["e1"]}))
    assert out == {"disabled_builtin_extensions": ["e1"]}
    assert invalidated == [True]
    assert ext_calls
