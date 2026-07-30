"""Locks the three-way per-round backend dispatch in
`runner_better_agent.py`, reconciled from two independently-built branches:

- Codex ChatGPT-subscription support (`provider_mode=="subscription"`,
  reached via the generic `OpenAIProvider` whose `KIND` is the fixed
  string "openai" — so `inputs["provider_kind"]=="openai"` at runtime).
- Claude subscription support (`provider_kind=="claude"` AND
  `provider_mode=="subscription"`, stamped by
  `ClaudeBetterAgentRunnerProvider`).

`_round_backend_for` is the single dispatch decision `_run` makes; this
test drives the REAL `_run()` end to end for all three input shapes
(generic OpenAI api_key, codex subscription, claude subscription) and
observes which leaf-level backend actually gets invoked, mocking only the
network boundary (`_stream_chat`, `runner_better_agent_codex_subscription.
stream_chat`, `runner_better_agent_claude_subscription.
one_round_claude_subscription`) — never `_round_backend_for`,
`_model_stream`, or `_one_round` themselves, since those ARE the dispatch
code under test. This is the test that would have caught the merge
conflict between the two branches' incompatible dispatch conditions.

Uses a temp BETTER_AGENT_HOME (paths.engage_test_home) so no real session
state is touched. No real network calls are made.
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

_TMP_HOME = tempfile.mkdtemp(prefix="ba_runner_three_way_dispatch_test_home_")
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)

import paths  # noqa: E402
paths.engage_test_home(_TMP_HOME)

import runner_better_agent as ba_runner  # noqa: E402
import runner_better_agent_claude_subscription as claude_sub  # noqa: E402
import runner_better_agent_codex_subscription as codex_sub  # noqa: E402


def _base_inputs(tmp_path: Path, *, provider_kind: str, provider_mode: str) -> dict[str, Any]:
    return {
        "_capability_plan": {"mcp_servers": []},
        "cwd": str(tmp_path),
        "model": "test-model",
        "provider_kind": provider_kind,
        "provider_mode": provider_mode,
        "prompt": "say hi",
    }


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


async def _fake_stream_chat(
    base_url: str, api_key: str, model: str, messages: list[dict],
    tools: list[dict], reasoning_effort: Optional[str] = None,
) -> AsyncIterator[dict]:
    yield {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}


async def _fake_codex_stream_chat(
    *, codex_home: Path, model: str, messages: list[dict], tools: list[dict],
    reasoning_effort: Optional[str] = None, session_id: str = "",
) -> AsyncIterator[dict]:
    yield {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}


async def _fake_claude_round(
    base_url: str, api_key: str, model: str, messages: list[dict],
    emitter: Any, run_dir: Any, tool_schemas: list[dict],
    reasoning_effort: Optional[str] = None,
    codex_home: Optional[Any] = None, app_session_id: str = "",
) -> tuple[Optional[str], list[dict], Optional[str], Optional[str], Optional[dict]]:
    return "stop", [], "hi", None, None


def test_claude_subscription_inputs_dispatch_to_claude_backend(tmp_path, monkeypatch):
    calls: list[str] = []

    async def spy_claude_round(*args, **kwargs):
        calls.append("claude")
        return await _fake_claude_round(*args, **kwargs)

    async def spy_stream_chat(*args, **kwargs):
        calls.append("openai")
        async for chunk in _fake_stream_chat(*args, **kwargs):
            yield chunk

    async def spy_codex_stream_chat(*args, **kwargs):
        calls.append("codex")
        async for chunk in _fake_codex_stream_chat(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(claude_sub, "one_round_claude_subscription", spy_claude_round)
    monkeypatch.setattr(ba_runner, "_stream_chat", spy_stream_chat)
    monkeypatch.setattr(codex_sub, "stream_chat", spy_codex_stream_chat)

    inputs = _base_inputs(tmp_path, provider_kind="claude", provider_mode="subscription")
    run_dir = _run_dir(tmp_path)
    rc = asyncio.run(ba_runner._run(run_dir, inputs))

    assert rc == 0
    assert calls == ["claude"]


def test_codex_subscription_inputs_dispatch_to_codex_backend(tmp_path, monkeypatch):
    calls: list[str] = []

    async def spy_claude_round(*args, **kwargs):
        calls.append("claude")
        return await _fake_claude_round(*args, **kwargs)

    async def spy_stream_chat(*args, **kwargs):
        calls.append("openai")
        async for chunk in _fake_stream_chat(*args, **kwargs):
            yield chunk

    async def spy_codex_stream_chat(*args, **kwargs):
        calls.append("codex")
        async for chunk in _fake_codex_stream_chat(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(claude_sub, "one_round_claude_subscription", spy_claude_round)
    monkeypatch.setattr(ba_runner, "_stream_chat", spy_stream_chat)
    monkeypatch.setattr(codex_sub, "stream_chat", spy_codex_stream_chat)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))

    # codex's actual runtime shape: provider_kind collapses to "openai"
    # (OpenAIProvider.KIND), distinguished from a real OpenAI api_key
    # provider only by provider_mode=="subscription".
    inputs = _base_inputs(tmp_path, provider_kind="openai", provider_mode="subscription")
    run_dir = _run_dir(tmp_path)
    rc = asyncio.run(ba_runner._run(run_dir, inputs))

    assert rc == 0
    assert calls == ["codex"]


def test_openai_api_key_inputs_dispatch_to_generic_backend(tmp_path, monkeypatch):
    calls: list[str] = []

    async def spy_claude_round(*args, **kwargs):
        calls.append("claude")
        return await _fake_claude_round(*args, **kwargs)

    async def spy_stream_chat(*args, **kwargs):
        calls.append("openai")
        async for chunk in _fake_stream_chat(*args, **kwargs):
            yield chunk

    async def spy_codex_stream_chat(*args, **kwargs):
        calls.append("codex")
        async for chunk in _fake_codex_stream_chat(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(claude_sub, "one_round_claude_subscription", spy_claude_round)
    monkeypatch.setattr(ba_runner, "_stream_chat", spy_stream_chat)
    monkeypatch.setattr(codex_sub, "stream_chat", spy_codex_stream_chat)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.test/v1")

    inputs = _base_inputs(tmp_path, provider_kind="openai", provider_mode="api_key")
    run_dir = _run_dir(tmp_path)
    rc = asyncio.run(ba_runner._run(run_dir, inputs))

    assert rc == 0
    assert calls == ["openai"]


@pytest.mark.parametrize(
    ("provider_kind", "provider_mode", "expected"),
    [
        ("claude", "subscription", ba_runner.BACKEND_CLAUDE_SUBSCRIPTION),
        ("openai", "subscription", ba_runner.BACKEND_CODEX_SUBSCRIPTION),
        ("openai", "api_key", ba_runner.BACKEND_OPENAI),
        ("claude", "api_key", ba_runner.BACKEND_OPENAI),
    ],
)
def test_round_backend_for_classifies_all_three_ways(provider_kind, provider_mode, expected):
    inputs = {"provider_kind": provider_kind, "provider_mode": provider_mode}
    assert ba_runner._round_backend_for(inputs) == expected
