#!/usr/bin/env python3
"""Unit owner for git_api.py.

git_api.py is a FastAPI router mounted at runtime by app_composition.wire()
(its only importer); no pytest module imports or owns it. This file is its
pytest owner.

Strategy (same shape as the runtime_*_api / harness_profiles_api owners):
- async route handlers driven directly via asyncio.run (no app/Request).
- collaborators patched at the module boundary: the `git_status_cache`
  singleton object and the imported `node_op` async function.
- Query params are passed explicitly (a Query() default resolves to the
  Param object on a direct call, not the string); the default-value contract
  is locked separately via signature inspection.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import git_api


# ---------- fakes ----------


class _CacheFake:
    """Stand-in for git_status_cache.cache: records calls, returns canned data."""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, str]] = []
        self.invalidate_calls: list[tuple[str, str]] = []
        self.get_return: object = {"status": "clean"}

    async def get(self, node_id: str, cwd: str) -> object:
        self.get_calls.append((node_id, cwd))
        return self.get_return

    def invalidate(self, node_id: str, cwd: str) -> None:
        self.invalidate_calls.append((node_id, cwd))


class _NodeOpFake:
    """Stand-in for node_op: records the (node_id, method, params) it was called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.return_value: object = {"ok": True}

    async def __call__(self, node_id: str, method: str, params: dict) -> object:
        self.calls.append((node_id, method, params))
        return self.return_value


@pytest.fixture
def cache_fake(monkeypatch):
    fake = _CacheFake()
    monkeypatch.setattr(git_api, "git_status_cache", fake)
    return fake


@pytest.fixture
def nodeop_fake(monkeypatch):
    fake = _NodeOpFake()
    # node_op is an imported function; patch the NAME on the importing module.
    monkeypatch.setattr(git_api, "node_op", fake)
    return fake


# ---------- get_git_status ----------


def test_get_git_status_returns_cache_result(cache_fake):
    cache_fake.get_return = {"status": "dirty", "ahead": 2}
    out = asyncio.run(git_api.get_git_status(cwd="/repo", node_id="primary"))
    assert out == {"status": "dirty", "ahead": 2}
    assert cache_fake.get_calls == [("primary", "/repo")]


def test_get_git_status_forwards_custom_node_id(cache_fake):
    asyncio.run(git_api.get_git_status(cwd="/r", node_id="worker-2"))
    assert cache_fake.get_calls == [("worker-2", "/r")]


# ---------- get_git_tree ----------


def test_get_git_tree_default_limit_drives_node_op(nodeop_fake):
    # limit passed explicitly: a Query() default resolves to the Param object
    # on a direct call, not the value. The 200 default is locked separately
    # in test_query_defaults_lock_contract.
    out = asyncio.run(git_api.get_git_tree(cwd="/repo", node_id="primary", limit=200))
    assert out == {"ok": True}
    assert nodeop_fake.calls == [
        ("primary", "get_git_tree", {"cwd": "/repo", "limit": 200})
    ]


def test_get_git_tree_custom_limit_and_node(nodeop_fake):
    asyncio.run(git_api.get_git_tree(cwd="/r", node_id="wn", limit=500))
    assert nodeop_fake.calls == [
        ("wn", "get_git_tree", {"cwd": "/r", "limit": 500})
    ]


# ---------- get_git_diff ----------


def test_get_git_diff_maps_path_to_file_path(nodeop_fake):
    out = asyncio.run(
        git_api.get_git_diff(path="src/a.py", cwd="/repo", node_id="primary")
    )
    assert out == {"ok": True}
    assert nodeop_fake.calls == [
        ("primary", "get_file_diff", {"file_path": "src/a.py", "cwd": "/repo"})
    ]


def test_get_git_diff_custom_node_id(nodeop_fake):
    asyncio.run(git_api.get_git_diff(path="b", cwd="/c", node_id="wn"))
    assert nodeop_fake.calls == [
        ("wn", "get_file_diff", {"file_path": "b", "cwd": "/c"})
    ]


# ---------- post handlers + _commit ----------


def test_post_git_commit_drives_commit_method(cache_fake, nodeop_fake):
    body = {"cwd": "/repo", "message": "ship", "node_id": "primary"}
    out = asyncio.run(git_api.post_git_commit(body))
    assert out == {"ok": True}
    assert nodeop_fake.calls == [
        ("primary", "git_commit", {"cwd": "/repo", "message": "ship"})
    ]
    # invalidate fires once before and once after the node_op call.
    assert cache_fake.invalidate_calls == [("primary", "/repo"), ("primary", "/repo")]


def test_post_git_commit_and_push_drives_push_method(cache_fake, nodeop_fake):
    asyncio.run(git_api.post_git_commit_and_push({"cwd": "/r", "message": "m"}))
    assert nodeop_fake.calls == [("primary", "git_commit_and_push", {"cwd": "/r", "message": "m"})]


def test_commit_defaults_node_id_to_primary(cache_fake, nodeop_fake):
    asyncio.run(git_api._commit({"cwd": "/r", "message": "m"}, "git_commit"))
    assert nodeop_fake.calls[0][0] == "primary"
    assert cache_fake.invalidate_calls[0][0] == "primary"


def test_commit_falsy_node_id_falls_back_to_primary(cache_fake, nodeop_fake):
    asyncio.run(git_api._commit({"node_id": "", "cwd": "/r", "message": "m"}, "git_commit"))
    assert nodeop_fake.calls[0][0] == "primary"


def test_commit_defaults_cwd_and_message_when_missing(cache_fake, nodeop_fake):
    asyncio.run(git_api._commit({}, "git_commit"))
    assert nodeop_fake.calls == [("primary", "git_commit", {"cwd": "", "message": ""})]
    assert cache_fake.invalidate_calls == [("primary", ""), ("primary", "")]


def test_commit_invalidates_before_and_after_node_op(cache_fake, nodeop_fake, monkeypatch):
    # Capture how many invalidates have fired by the time node_op runs.
    invalidate_at = []

    async def tracking_node_op(node_id, method, params):
        invalidate_at.append(list(cache_fake.invalidate_calls))
        return {"ok": True}

    monkeypatch.setattr(git_api, "node_op", tracking_node_op)
    asyncio.run(git_api._commit({"cwd": "/r", "message": "m"}, "git_commit"))
    # Exactly one invalidate has fired when node_op runs; the second follows it.
    assert invalidate_at == [[("primary", "/r")]]
    assert cache_fake.invalidate_calls == [("primary", "/r"), ("primary", "/r")]


def test_commit_returns_node_op_result(cache_fake, nodeop_fake):
    nodeop_fake.return_value = {"committed": "abc123"}
    out = asyncio.run(git_api._commit({"cwd": "/r"}, "git_commit"))
    assert out == {"committed": "abc123"}


# ---------- Query default-value contract ----------


def test_query_defaults_lock_contract():
    """The frontend relies on these defaults; lock them at the signature level."""
    status_params = inspect.signature(git_api.get_git_status).parameters
    assert _query_default(status_params["node_id"]) == "primary"

    tree_params = inspect.signature(git_api.get_git_tree).parameters
    assert _query_default(tree_params["node_id"]) == "primary"
    assert _query_default(tree_params["limit"]) == 200

    diff_params = inspect.signature(git_api.get_git_diff).parameters
    assert _query_default(diff_params["node_id"]) == "primary"


def _query_default(param: inspect.Parameter):
    """Resolve a Query()-wrapped default to its declared value."""
    default = param.default
    return getattr(default, "default", default)
