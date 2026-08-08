"""Unit tests for the ``JsonSessionRootRepository`` delegation contract.

``JsonSessionRootRepository`` is a pure adapter: every method forwards to a
``session_store`` function and returns its result. ``SessionManager`` depends
on this facade (``test_session_repository_boundary.py`` AST-asserts no kernel
fallbacks), but no pytest-collected test exercised the real delegation — the
only other touchers subclass the ``SessionRootRepository`` Protocol with
fakes. These tests lock the wiring: each method must call the correct
``session_store`` function with the forwarded arguments and pass through its
return value unchanged.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_session_repository_unit.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-session-repository-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import paths  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager  # noqa: E402
from session_repository import JsonSessionRootRepository  # noqa: E402


def _spy(monkeypatch, name):
    """Wrap ``session_store.<name>`` to record its call, delegate, expose result."""
    original = getattr(session_store, name)
    state = {"calls": [], "last": object()}

    def _wrapper(*args, **kwargs):
        state["calls"].append((args, kwargs))
        result = original(*args, **kwargs)
        state["last"] = result
        return result

    monkeypatch.setattr(session_store, name, _wrapper)
    return state


def _make_root():
    root = manager.create(
        name="repository-unit",
        model="sonnet",
        cwd="/tmp/session-repository-unit",
        orchestration_mode="native",
        source="cli",
    )
    manager.flush_pending_persists()
    return root


def test_storage_identity_delegates_to_sessions_dir(monkeypatch):
    spy = _spy(monkeypatch, "_sessions_dir")
    identity = JsonSessionRootRepository().storage_identity()
    assert spy["calls"] == [((), {})]
    assert identity is spy["last"]
    assert isinstance(identity, Path)
    assert paths.ba_home() in identity.parents


def test_register_writer_guard_delegates(monkeypatch):
    spy = _spy(monkeypatch, "register_root_writer_guard")
    guard = lambda key, work: work()  # noqa: E731
    JsonSessionRootRepository().register_writer_guard(guard)
    assert spy["calls"] == [((guard,), {})]
    assert session_store._root_writer_guard is guard


def test_resolve_root_ids_delegates(monkeypatch):
    root = _make_root()
    spy = _spy(monkeypatch, "_resolve_root_ids")
    resolved = JsonSessionRootRepository().resolve_root_ids((root["id"],))
    assert len(spy["calls"]) == 1
    args, kwargs = spy["calls"][0]
    assert kwargs == {}
    assert args == ((root["id"],),)
    assert resolved is spy["last"]
    assert resolved == {root["id"]: root["id"]}


def test_root_version_delegates_to_fingerprint(monkeypatch):
    root = _make_root()
    spy = _spy(monkeypatch, "session_file_fingerprint")
    version = JsonSessionRootRepository().root_version(root["id"])
    assert spy["calls"] == [((root["id"],), {})]
    assert version is spy["last"]
    assert isinstance(version, tuple) and len(version) == 5


def test_read_root_delegates(monkeypatch):
    root = _make_root()
    spy = _spy(monkeypatch, "get_root_tree")
    tree = JsonSessionRootRepository().read_root(root["id"])
    assert spy["calls"] == [((root["id"],), {})]
    assert tree is spy["last"]
    assert tree["id"] == root["id"]


def test_copy_persistable_root_delegates(monkeypatch):
    root = _make_root()
    full = session_store.get_root_tree(root["id"])
    spy = _spy(monkeypatch, "copy_persistable_tree")
    copy = JsonSessionRootRepository().copy_persistable_root(full)
    assert spy["calls"] == [((full,), {})]
    assert copy is spy["last"]
    assert copy["id"] == full["id"]
    # copy_persistable_tree strips projection-only fields: the draft fields
    # present on the in-memory tree are absent from the persistable copy,
    # proving the real normalization ran (not a passthrough).
    assert "draft_images" in full
    assert "draft_images" not in copy


def test_write_root_forwards_all_kwargs(monkeypatch):
    root = _make_root()
    full = session_store.get_root_tree(root["id"])
    full["name"] = "repository-unit-renamed"
    spy = _spy(monkeypatch, "write_session_full")
    version = JsonSessionRootRepository().write_root(
        full,
        bump_updated_at=False,
        preserve_projection_fields=True,
        already_persistable=True,
    )
    assert spy["calls"] == [
        (
            (full,),
            {
                "bump_updated_at": False,
                "preserve_projection_fields": True,
                "already_persistable": True,
            },
        )
    ]
    assert version is spy["last"]


def test_write_root_defaults_match_session_store(monkeypatch):
    root = _make_root()
    full = session_store.get_root_tree(root["id"])
    spy = _spy(monkeypatch, "write_session_full")
    JsonSessionRootRepository().write_root(full)
    assert spy["calls"] == [
        (
            (full,),
            {
                "bump_updated_at": True,
                "preserve_projection_fields": False,
                "already_persistable": False,
            },
        )
    ]


def test_delete_root_delegates(monkeypatch):
    root = _make_root()
    spy = _spy(monkeypatch, "delete_session")
    deleted = JsonSessionRootRepository().delete_root(root["id"])
    assert spy["calls"] == [((root["id"],), {})]
    assert deleted is spy["last"]
    assert deleted is True
