"""Regression: team-mode primary turns must receive the BOOTSTRAP prompt.

The coordinator only delegates (instead of doing project work itself) when
its model-facing prompt carries the manager BOOTSTRAP + `<known_workers>`
block. `wrap_cli_prompt` produces that block, and `run_primary` applies it
only when `cli_prompt is None` — an explicit override bypasses the wrap.

A bug in `handle_prompt` (`cli_prompt = cli_prompt or prompt`) defaulted
`cli_prompt` to the raw user prompt for every normal send, so `run_primary`
always saw a non-None `cli_prompt`, the wrap was skipped, and the bootstrap
never reached the model. These tests lock the fix by capturing the
`cli_prompt` that reaches `run_turn` (the model-facing string) and asserting
the bootstrap marker is present for unoverridden team turns and absent when
a caller passes an explicit override.

No claude CLI subprocess — `turn_manager.run_turn` is stubbed to capture.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-team-bootstrap-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def teardown_module():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


async def _noop_ws_callback(_frame):
    return None


def _drive(monkeypatch, *, orchestration_mode, cli_prompt):
    """Run one handle_prompt turn; return the cli_prompt that reached run_turn."""
    session = session_manager.create(
        name="team session",
        cwd="/repo",
        orchestration_mode=orchestration_mode,
    )
    coordinator = Coordinator()
    captured: dict = {}

    async def fake_run_turn(**kwargs):
        captured["cli_prompt"] = kwargs.get("cli_prompt")
        return None

    monkeypatch.setattr(coordinator.turn_manager, "run_turn", fake_run_turn)

    async def run():
        await coordinator.handle_prompt(
            prompt="ship the feature",
            app_session_id=session["id"],
            model="any-model",
            cwd="/repo",
            ws_callback=_noop_ws_callback,
            cli_prompt=cli_prompt,
        )

    asyncio.run(run())
    return captured["cli_prompt"]


def test_team_turn_without_override_receives_bootstrap(monkeypatch):
    sent = _drive(monkeypatch, orchestration_mode="team", cli_prompt=None)
    # The bootstrap wrap is the only producer of <user_prompt>; its absence
    # is exactly the pre-fix bug.
    assert "<user_prompt>" in sent
    assert "ship the feature" in sent


def test_team_turn_with_explicit_override_skips_wrap(monkeypatch):
    override = "my pre-formatted team-message body"
    sent = _drive(monkeypatch, orchestration_mode="team", cli_prompt=override)
    assert sent == override
    assert "<user_prompt>" not in sent


def test_native_turn_without_override_is_passthrough(monkeypatch):
    sent = _drive(monkeypatch, orchestration_mode="native", cli_prompt=None)
    # wrap_cli_prompt is identity for native: raw prompt reaches the model.
    assert sent == "ship the feature"


def test_supervisor_direct_turn_without_override_keeps_prompt(monkeypatch):
    """The supervisor-direct branch bypasses run_primary, so it must default
    None→prompt itself or the model receives an empty prompt (regression
    caught in ADV review of the handle_prompt fix)."""
    import extension_store

    session = session_manager.create(
        name="supervisor session",
        cwd="/repo",
        orchestration_mode="native",
    )
    real_get = session_manager.get

    def supervisor_enabled_get(sid):
        s = real_get(sid)
        if s and sid == session["id"]:
            s = dict(s)
            s["supervisor_enabled"] = True
            s["supervisor_agent_session_id"] = "sup-sid"
        return s

    monkeypatch.setattr(session_manager, "get", supervisor_enabled_get)
    monkeypatch.setattr(
        extension_store,
        "runtime_not_ready_message",
        lambda *a, **k: None,
    )

    coordinator = Coordinator()
    captured: dict = {}

    async def fake_run_turn(**kwargs):
        captured["cli_prompt"] = kwargs.get("cli_prompt")

    monkeypatch.setattr(coordinator, "run_turn", fake_run_turn)

    async def run():
        await coordinator.handle_prompt(
            prompt="review this",
            app_session_id=session["id"],
            model="any-model",
            cwd="/repo",
            ws_callback=_noop_ws_callback,
            send_target="supervisor",
            cli_prompt=None,
        )

    asyncio.run(run())
    assert captured["cli_prompt"] == "review this"

