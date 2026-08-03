from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

import _test_home

_test_home.isolate("bc_provider_steer_files_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provider_codex import CodexProvider, RunState as CodexRunState  # noqa: E402
from provider_claude_better_agent_runner import (  # noqa: E402
    ClaudeBetterAgentRunnerProvider,
)
from provider_openai import OpenAIProvider  # noqa: E402
from provider_session_events import RunState as SessionEventsRunState  # noqa: E402


class _Popen:
    def poll(self):
        return None


def _file() -> dict:
    raw = b"steer attachment"
    return {
        "name": "steer.txt",
        "media_type": "text/plain",
        "size": len(raw),
        "data": base64.b64encode(raw).decode("ascii"),
    }


@pytest.mark.parametrize("kind", ["codex", "openai", "claude_ba"])
def test_provider_steer_inbox_preserves_files(tmp_path, kind):
    run_dir = tmp_path / kind
    run_dir.mkdir()
    if kind == "codex":
        provider = CodexProvider({"id": "codex-test", "kind": "codex"})
        state = {"session_id": "session-1", "turn_id": "turn-1"}
        run_state = CodexRunState(
            run_id="run-1",
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app-1",
            queue=asyncio.Queue(),
        )
    elif kind == "openai":
        provider = OpenAIProvider({"id": "openai-test", "kind": "openai"})
        state = {"session_id": "session-1"}
        run_state = SessionEventsRunState(
            run_id="run-1",
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app-1",
            queue=asyncio.Queue(),
        )
    else:
        provider = ClaudeBetterAgentRunnerProvider({
            "id": "claude-ba-test",
            "kind": "claude",
            "mode": "subscription",
        })
        state = {"session_id": "session-1"}
        run_state = SessionEventsRunState(
            run_id="run-1",
            run_dir=run_dir,
            popen=_Popen(),
            mode="native",
            app_session_id="app-1",
            queue=asyncio.Queue(),
        )
    provider._runs["run-1"] = run_state
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    assert provider.steer_run("run-1", "", files=[_file()]) is True
    payload = json.loads((run_dir / "steer.jsonl").read_text(encoding="utf-8"))
    assert payload == {"prompt": "", "images": [], "files": [_file()]}
