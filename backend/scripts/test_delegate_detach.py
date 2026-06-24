"""The `delegate` tool: detached fire-and-forget handoff.

delegate is mssg with detach=True, so submit_team_message must NOT register a
turn-join waiter — the dispatched work runs independently and does not hold the
sender's turn open. Contrast with mssg (detach=False), which joins.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-delegate-detach-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Coordinator
from session_manager import manager as session_manager


def _make_coord():
    sender = session_manager.create(name="sender", cwd="/repo", orchestration_mode="manager")
    target = session_manager.create(name="target", cwd="/repo", orchestration_mode="native")
    coord = Coordinator()
    coord.submit_prompt = lambda sid, params: params["_queued_id"]  # type: ignore
    coord.turn_manager.has_active_turn = lambda sid: True  # type: ignore
    registrations: list[dict] = []
    coord.register_mssg_turn_waiter = lambda **kw: registrations.append(kw)  # type: ignore
    return coord, sender, target, registrations


def test_mssg_registers_turn_join():
    coord, sender, target, registrations = _make_coord()
    asyncio.run(coord.submit_team_message(
        sender_session_id=sender["id"], target_session_id=target["id"], message="hi",
    ))
    assert len(registrations) == 1, "mssg (detach=False) must join the turn"
    assert registrations[0]["target_session_id"] == target["id"]


def test_delegate_does_not_register_turn_join():
    coord, sender, target, registrations = _make_coord()
    asyncio.run(coord.submit_team_message(
        sender_session_id=sender["id"], target_session_id=target["id"],
        message="off-topic tangent", detach=True,
    ))
    assert registrations == [], "delegate (detach=True) must NOT join the turn"
