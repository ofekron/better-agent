from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-delegate-fork-")

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from session_manager import manager as session_manager  # noqa: E402
from stores import worker_store  # noqa: E402


def test_delegate_fork_for_unregistered_target_is_internal_empty_branch():
    events: list[tuple[str, str]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(
            lambda sid, change: events.append((sid, change.get("kind")))
        )
    caller = session_manager.create(
        name="caller",
        cwd="/tmp/project",
        orchestration_mode="native",
        model="model",
        source="test",
    )
    target = session_manager.create(
        name="target",
        cwd="/tmp/project",
        orchestration_mode="native",
        model="model",
        source="test",
    )
    session_manager.append_user_msg(
        target["id"],
        {
            "id": "target-user-msg",
            "role": "user",
            "content": "history must not be copied",
            "events": [],
        },
    )
    session_manager.set_agent_sid(
        target["id"],
        "native",
        "target-agent-sid",
        bump_updated_at=False,
    )

    fork = session_manager.create_delegate_fork(
        parent_agent_session_id=target["id"],
        caller_agent_session_id=caller["id"],
        parent_agent_sid_at_fork="target-agent-sid",
        parent_line_count_at_fork=7,
        orchestration_mode="native",
    )

    persisted = session_manager.get(fork["id"])
    assert persisted is not None
    assert persisted["kind"] == "delegate_fork"
    assert persisted["parent_session_id"] == target["id"]
    assert persisted["caller_agent_session_id"] == caller["id"]
    assert persisted["forked_from_agent_sid"] == "target-agent-sid"
    assert persisted["parent_line_count_at_fork"] == 7
    assert persisted["messages"] == []
    assert worker_store.list_workers("/tmp/project") == []
    assert (fork["id"], "delegate_fork_created") in events
    assert all(kind != "forked" for _sid, kind in events)


if __name__ == "__main__":
    test_delegate_fork_for_unregistered_target_is_internal_empty_branch()
    print("PASS: delegate fork for unregistered target is internal empty branch")
