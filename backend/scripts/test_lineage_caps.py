"""Locks the lineage caps: session creation depth and live descendants per
root are enforced in session_manager's fork-tree creators, driven by
user_prefs settings, and rejections surface as LineageCapExceeded
(a ValueError, so HTTP/MCP boundaries convert it to an agent-visible error).

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_lineage_caps.py
"""

from __future__ import annotations

import os
import sys

import pytest

import _test_home

_test_home.isolate("bc-test-lineage-caps-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import user_prefs  # noqa: E402
from session_manager import (  # noqa: E402
    LineageCapExceeded,
    manager as session_manager,
)


def _new_root(name: str) -> dict:
    return session_manager.create(
        name=name, model="sonnet", cwd="/tmp/lineage-caps",
        orchestration_mode="native", source="cli",
    )


def _new_child(parent_id: str, name: str) -> dict:
    return session_manager.create_sub_session(
        parent_session_id=parent_id, name=name, cwd="/tmp/lineage-caps",
    )


@pytest.fixture(autouse=True)
def _reset_caps():
    user_prefs.set_session_creation_depth_cap(
        user_prefs.DEFAULT_SESSION_CREATION_DEPTH_CAP
    )
    user_prefs.set_session_max_live_descendants(
        user_prefs.DEFAULT_SESSION_MAX_LIVE_DESCENDANTS
    )
    session_manager.set_live_session_probe(lambda sid: False)
    yield


def test_creation_depth_cap_rejects_deep_nesting() -> None:
    user_prefs.set_session_creation_depth_cap(2)
    root = _new_root("depth-root")
    child = _new_child(root["id"], "depth-1")
    grandchild = _new_child(child["id"], "depth-2")

    with pytest.raises(LineageCapExceeded, match="depth cap exceeded"):
        _new_child(grandchild["id"], "depth-3")

    # Raising the cap unblocks the same creation without a restart.
    user_prefs.set_session_creation_depth_cap(3)
    assert _new_child(grandchild["id"], "depth-3")["id"]


def test_depth_cap_zero_disables_check() -> None:
    user_prefs.set_session_creation_depth_cap(0)
    node = _new_root("depth-unlimited")
    for i in range(8):
        node = _new_child(node["id"], f"deep-{i}")
    assert node["id"]


def test_live_descendants_cap_counts_only_live_sessions() -> None:
    user_prefs.set_session_max_live_descendants(2)
    root = _new_root("desc-root")
    first = _new_child(root["id"], "desc-1")
    second = _new_child(root["id"], "desc-2")

    live = {first["id"], second["id"]}
    session_manager.set_live_session_probe(lambda sid: sid in live)
    with pytest.raises(LineageCapExceeded, match="live descendant"):
        _new_child(root["id"], "desc-3")

    # One descendant finishing frees a slot.
    live.discard(second["id"])
    assert _new_child(root["id"], "desc-3")["id"]


def test_descendants_cap_skipped_without_probe() -> None:
    user_prefs.set_session_max_live_descendants(1)
    session_manager._live_session_probe = None
    root = _new_root("probe-less-root")
    assert _new_child(root["id"], "a")["id"]
    assert _new_child(root["id"], "b")["id"]


def test_fork_path_enforces_depth_cap() -> None:
    user_prefs.set_session_creation_depth_cap(1)
    root = _new_root("fork-root")
    # Forking requires the parent to have taken a turn (agent sid set).
    session_manager.set_agent_sid(root["id"], "native", "agent-sid-root")
    fork = session_manager.fork(root["id"], name="fork-1")
    session_manager.set_agent_sid(fork["id"], "native", "agent-sid-fork")
    with pytest.raises(LineageCapExceeded):
        session_manager.fork(fork["id"], name="fork-2")


def test_guard_settings_bounds_and_get_all() -> None:
    with pytest.raises(ValueError):
        user_prefs.set_sync_wait_depth_cap(-1)
    with pytest.raises(ValueError):
        user_prefs.set_sync_wait_depth_cap(user_prefs.MAX_SYNC_WAIT_DEPTH_CAP + 1)
    with pytest.raises(ValueError):
        user_prefs.set_session_creation_depth_cap(True)

    user_prefs.set_sync_wait_depth_cap(5)
    user_prefs.set_session_creation_depth_cap(7)
    user_prefs.set_session_max_live_descendants(9)
    prefs = user_prefs.get_all()
    assert prefs["sync_wait_depth_cap"] == 5
    assert prefs["session_creation_depth_cap"] == 7
    assert prefs["session_max_live_descendants"] == 9
