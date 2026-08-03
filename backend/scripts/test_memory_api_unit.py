"""Unit coverage for memory_api.py.

Calls the pure validator and the async route handlers directly with faked
dependencies (no live backend, no provider turn) to cover every branch of
the memory proposal validation and the memory HTTP surface.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import internal_guards
import memory_api
from memory_store import MemoryStoreError


def _run(coro):
    return asyncio.run(coro)


class FakeMemoryStore:
    """Stand-in for memory_store (all methods sync, wrapped in to_thread).

    Exposes MemoryStoreError so `memory_store.MemoryStoreError` exception
    clauses in memory_api resolve against the fake module object.
    """

    MemoryStoreError = MemoryStoreError

    def __init__(self):
        self._by_scope = {}          # (scope_type, scope_path) -> list
        self._scopes = []
        self._memories_for_cwd = {}
        self._write_returns = {"id": "mem-1", "slug": "s", "ok": True}
        self._delete_returns = True
        self._write_raises = None
        self._delete_raises = None
        self.writes = []
        self.deletes = []

    def list_memories(self, *, scope_type, scope_path):
        return self._by_scope.get((scope_type, scope_path), [])

    def list_scopes(self):
        return self._scopes

    def memories_for_cwd(self, cwd):
        return self._memories_for_cwd.get(cwd, {})

    def write_memory(self, *, scope_type, scope_path, slug, description, mem_type, content):
        if self._write_raises:
            raise self._write_raises
        rec = {
            **self._write_returns,
            "scope_type": scope_type,
            "scope_path": scope_path,
            "slug": slug,
            "description": description,
            "type": mem_type,
            "content": content,
        }
        self.writes.append(rec)
        return rec

    def delete_memory(self, *, scope_type, scope_path, slug):
        if self._delete_raises:
            raise self._delete_raises
        self.deletes.append((scope_type, scope_path, slug))
        return self._delete_returns


@pytest.fixture(autouse=True)
def _reset_authority():
    yield
    internal_guards.configure(None)  # restore default-invalid, no cross-test leak


@pytest.fixture
def store(monkeypatch):
    fake = FakeMemoryStore()
    monkeypatch.setattr(memory_api, "memory_store", fake)
    internal_guards.configure(lambda: object())  # authority valid by default
    return fake


def _valid(**overrides):
    base = {
        "action": "add",
        "name": "my-mem",
        "description": "a desc",
        "type": "user",
        "content": "body",
        "scope_type": "global",
        "scope_path": "",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------- validate_memory_proposal

def test_validate_add_global_clears_scope_path(store):
    p = memory_api.validate_memory_proposal(_valid(scope_path="should-be-dropped"))
    assert p["action"] == "add"
    assert p["scope_type"] == "global"
    assert p["scope_path"] == ""
    assert p["name"] == "my-mem"


def test_validate_edit_requires_target_slug(store):
    p = memory_api.validate_memory_proposal(
        _valid(action="edit", target_slug="old-slug"))
    assert p["action"] == "edit"
    assert p["target_slug"] == "old-slug"


def test_validate_edit_missing_target_slug(store):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(action="edit"))
    assert exc.value.status_code == 400


def test_validate_edit_bad_target_slug(store):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(action="edit", target_slug="bad space"))
    assert exc.value.status_code == 400


def test_validate_action_defaults_to_add(store):
    p = memory_api.validate_memory_proposal(_valid(action=None))
    assert p["action"] == "add"


@pytest.mark.parametrize("action", ["delete", "remove"])
def test_validate_action_invalid(store, action):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(action=action))
    assert exc.value.status_code == 400


def test_validate_non_dict_raw(store):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(["not", "a", "dict"])
    assert exc.value.status_code == 400


@pytest.mark.parametrize("bad_name", ["", "has space", "-leading", "under_score", "x" * 81])
def test_validate_bad_name(store, bad_name):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(name=bad_name))
    assert exc.value.status_code == 400


def test_validate_name_lowercased(store):
    p = memory_api.validate_memory_proposal(_valid(name="My-Mem"))
    assert p["name"] == "my-mem"


def test_validate_description_required(store):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(description=""))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("desc", ["line1\nline2", "line1\rline2"])
def test_validate_description_rejects_newline_frontmatter_injection(store, desc):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(description=desc))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("bad_type", ["", "trash", "USER"])
def test_validate_bad_type(store, bad_type):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(type=bad_type))
    assert exc.value.status_code == 400


def test_validate_content_required(store):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(content="   "))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("bad_scope", ["", "global-ish", "universe"])
def test_validate_bad_scope_type(store, bad_scope):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(scope_type=bad_scope))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("scope", ["project", "folder"])
def test_validate_scoped_requires_scope_path(store, scope):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(scope_type=scope, scope_path=""))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("sp", ["a\nb", "a\rb"])
def test_validate_scope_path_rejects_newline(store, sp):
    with pytest.raises(HTTPException) as exc:
        memory_api.validate_memory_proposal(_valid(scope_type="project", scope_path=sp))
    assert exc.value.status_code == 400


def test_validate_truncates_long_fields(store):
    p = memory_api.validate_memory_proposal(_valid(
        scope_type="project",
        scope_path="/p/" + "x" * 2000,
        description="d" * 500,
        content="c" * 10000,
    ))
    assert len(p["description"]) == 300
    assert len(p["content"]) == 8000
    assert len(p["scope_path"]) == 1000


# ---------------------------------------------------------- get_all_memory_scopes

def test_get_all_memory_scopes_aggregates(store):
    store._by_scope[("global", "")] = [{"slug": "g"}]
    store._scopes = [{"type": "project", "path": "/repo"}]
    store._by_scope[("project", "/repo")] = [{"slug": "p"}]

    res = _run(memory_api.get_all_memory_scopes())
    assert res["global"] == [{"slug": "g"}]
    assert res["scopes"] == [
        {"type": "project", "path": "/repo", "memories": [{"slug": "p"}]}
    ]


# ------------------------------------------------------------ get_memory_scopes

def test_get_memory_scopes_empty_cwd_returns_global(store):
    store._by_scope[("global", "")] = [{"slug": "g"}]
    res = _run(memory_api.get_memory_scopes(cwd=None))
    assert res == {"scopes": {"global": [{"slug": "g"}]}}


def test_get_memory_scopes_with_cwd(store):
    store._memories_for_cwd["/repo"] = {"global": [{"slug": "g"}]}
    res = _run(memory_api.get_memory_scopes(cwd="/repo"))
    assert res == {"scopes": {"global": [{"slug": "g"}]}}


# ------------------------------------------------------------------- put_memory

def test_put_memory_success(store):
    res = _run(memory_api.put_memory("global", "new-mem", {
        "description": "d", "type": "user", "content": "c", "scope_path": "",
    }))
    assert res["success"] is True
    assert store.writes[0]["slug"] == "new-mem"
    assert store.writes[0]["scope_type"] == "global"


def test_put_memory_invalid_proposal_400(store):
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.put_memory("global", "new-mem", {
            "description": "d", "type": "bogus", "content": "c",
        }))
    assert exc.value.status_code == 400


def test_put_memory_store_error_400(store):
    store._write_raises = MemoryStoreError("disk full")
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.put_memory("global", "new-mem", {
            "description": "d", "type": "user", "content": "c",
        }))
    assert exc.value.status_code == 400


# ------------------------------------------------------------- delete_memory_route

def test_delete_memory_route_success(store):
    store._delete_returns = True
    res = _run(memory_api.delete_memory_route("global", "m", scope_path=""))
    assert res == {"success": True}
    assert store.deletes == [("global", "", "m")]


def test_delete_memory_route_not_found_404(store):
    store._delete_returns = False
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.delete_memory_route("global", "ghost"))
    assert exc.value.status_code == 404


def test_delete_memory_route_store_error_400(store):
    store._delete_raises = MemoryStoreError("locked")
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.delete_memory_route("global", "m"))
    assert exc.value.status_code == 400


# ----------------------------------------------------------- internal_memory_write

def test_internal_memory_write_unauthorized_403(store, monkeypatch):
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.internal_memory_write({"memory_proposal": _valid()}, "tok"))
    assert exc.value.status_code == 403


def test_internal_memory_write_success(store):
    res = _run(memory_api.internal_memory_write(
        {"memory_proposal": _valid(name="fresh")}, "tok"))
    assert res["success"] is True
    assert store.writes[0]["slug"] == "fresh"


def test_internal_memory_write_edit_uses_target_slug(store):
    res = _run(memory_api.internal_memory_write(
        {"memory_proposal": _valid(action="edit", name="new-name", target_slug="old-name")},
        "tok"))
    assert res["success"] is True
    assert store.writes[0]["slug"] == "old-name"


def test_internal_memory_write_invalid_proposal(store):
    res = _run(memory_api.internal_memory_write(
        {"memory_proposal": _valid(type="bogus")}, "tok"))
    assert res["success"] is False
    assert "error" in res


def test_internal_memory_write_store_error(store):
    store._write_raises = MemoryStoreError("nope")
    res = _run(memory_api.internal_memory_write(
        {"memory_proposal": _valid()}, "tok"))
    assert res["success"] is False
    assert "error" in res


# ------------------------------------------------------------ internal_memory_list

def test_internal_memory_list_unauthorized_403(store, monkeypatch):
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.internal_memory_list({"cwd": "/r"}, "tok"))
    assert exc.value.status_code == 403


def test_internal_memory_list_missing_cwd(store):
    res = _run(memory_api.internal_memory_list({"cwd": ""}, "tok"))
    assert res == {"success": False, "error": "cwd is required"}


def test_internal_memory_list_success(store):
    store._memories_for_cwd["/r"] = {"global": [{"slug": "g"}]}
    res = _run(memory_api.internal_memory_list({"cwd": "/r"}, "tok"))
    assert res == {"success": True, "scopes": {"global": [{"slug": "g"}]}}


# ---------------------------------------------------------- internal_memory_delete

def test_internal_memory_delete_unauthorized_403(store, monkeypatch):
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)
    with pytest.raises(HTTPException) as exc:
        _run(memory_api.internal_memory_delete(
            {"scope_type": "global", "scope_path": "", "slug": "m"}, "tok"))
    assert exc.value.status_code == 403


def test_internal_memory_delete_success(store):
    store._delete_returns = True
    res = _run(memory_api.internal_memory_delete(
        {"scope_type": "global", "scope_path": "", "slug": "m"}, "tok"))
    assert res == {"success": True}


def test_internal_memory_delete_not_found(store):
    store._delete_returns = False
    res = _run(memory_api.internal_memory_delete(
        {"scope_type": "global", "slug": "ghost"}, "tok"))
    assert res == {"success": False}


def test_internal_memory_delete_store_error(store):
    store._delete_raises = MemoryStoreError("busy")
    res = _run(memory_api.internal_memory_delete(
        {"scope_type": "global", "slug": "m"}, "tok"))
    assert res["success"] is False
    assert "error" in res
