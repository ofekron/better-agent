from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchs.supervisor._primary as primary  # noqa: E402
from orchs.supervisor import run_primary_turn  # noqa: E402


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(primary.session_manager, "get", lambda sid: session)


def _patch_strategy(monkeypatch):
    """Capture the mode chosen and return the fake strategy whose
    run_primary is awaited by the dispatcher."""
    chosen: dict = {}
    strategy = MagicMock()
    strategy.run_primary = AsyncMock()

    def fake_get_strategy(mode):
        chosen["mode"] = mode
        return strategy

    monkeypatch.setattr("orchs.get_strategy", fake_get_strategy)
    return strategy, chosen


def test_missing_session_warns_and_returns_without_dispatch(monkeypatch, caplog):
    _patch_session(monkeypatch, None)
    strategy, _ = _patch_strategy(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=primary.logger.name):
        asyncio.run(
            run_primary_turn(
                coordinator=MagicMock(),
                app_session_id="ghost",
                prompt="p",
                ws_callback=AsyncMock(),
            )
        )

    assert "ghost" in caplog.text
    assert "missing session" in caplog.text
    strategy.run_primary.assert_not_awaited()


def test_dispatches_with_explicit_orchestration_mode(monkeypatch):
    session = {"orchestration_mode": "manager", "model": "gpt-x", "cwd": "/repo"}
    _patch_session(monkeypatch, session)
    strategy, chosen = _patch_strategy(monkeypatch)

    coordinator = MagicMock()
    ws_callback = AsyncMock()

    asyncio.run(
        run_primary_turn(
            coordinator=coordinator,
            app_session_id="s1",
            prompt="hello",
            ws_callback=ws_callback,
            images=["i"],
            files=["f"],
            source="src",
        )
    )

    assert chosen["mode"] == "manager"
    strategy.run_primary.assert_awaited_once()
    args, kwargs = strategy.run_primary.call_args
    assert args[0] is coordinator
    assert kwargs["session"] is session
    assert kwargs["prompt"] == "hello"
    assert kwargs["app_session_id"] == "s1"
    assert kwargs["model"] == "gpt-x"
    assert kwargs["cwd"] == "/repo"
    assert kwargs["ws_callback"] is ws_callback
    assert kwargs["images"] == ["i"]
    assert kwargs["files"] == ["f"]
    assert kwargs["source"] == "src"


def test_mode_falls_back_to_native_when_unset(monkeypatch):
    # Non-empty session (so the `not session` guard is False) with no
    # orchestration_mode and falsy model/cwd → mode falls back to "native"
    # and model/cwd forward as "".
    _patch_session(monkeypatch, {"model": None, "cwd": None})
    strategy, chosen = _patch_strategy(monkeypatch)

    asyncio.run(
        run_primary_turn(
            coordinator=MagicMock(),
            app_session_id="s2",
            prompt="p",
            ws_callback=AsyncMock(),
        )
    )

    assert chosen["mode"] == "native"
    _, kwargs = strategy.run_primary.call_args
    assert kwargs["model"] == ""
    assert kwargs["cwd"] == ""
