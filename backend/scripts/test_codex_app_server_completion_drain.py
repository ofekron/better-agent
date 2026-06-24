#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from runner_codex import _AppServerProcess  # noqa: E402


class _FakeStdout:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = [
            (json.dumps(row) + "\n").encode("utf-8")
            for row in rows
        ]

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if not self._rows:
            return b""
        return self._rows.pop(0)


class _FakeProc:
    def __init__(self, rows: list[dict]) -> None:
        self.pid = 12345
        self.returncode = None
        self.stdin = None
        self.stderr = None
        self.stdout = _FakeStdout(rows)


async def _collect(client: _AppServerProcess) -> list[dict]:
    rows: list[dict] = []
    async for raw in client.stdout:
        rows.append(json.loads(raw))
    return rows


async def _test_completion_drains_adjacent_item() -> None:
    rows = [
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "completed", "usage": {}}},
        },
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "item-final",
                    "type": "agentMessage",
                    "text": "FINAL",
                },
            },
        },
    ]
    with tempfile.TemporaryDirectory() as tmp:
        proc = _FakeProc(rows)
        client = _AppServerProcess(proc, Path(tmp))
        try:
            mapped = await asyncio.wait_for(_collect(client), timeout=2)
        finally:
            client._steer_task.cancel()
            try:
                await client._steer_task
            except asyncio.CancelledError:
                pass
        types = [row.get("type") for row in mapped]
        assert types == ["turn.completed"], types


async def _test_collab_agent_item_type_maps_to_normalized_name() -> None:
    rows = [
        {
            "method": "item/started",
            "params": {
                "threadId": "root-thread",
                "turnId": "turn-1",
                "item": {
                    "id": "collab_1",
                    "type": "collabAgentToolCall",
                    "tool": "explorer",
                    "status": "running",
                    "senderThreadId": "root-thread",
                    "receiverThreadIds": ["child-thread"],
                    "prompt": "Find Codex subagent event paths.",
                    "model": "gpt-5.4-mini",
                    "reasoningEffort": "medium",
                    "agentsStates": {},
                },
            },
        },
    ]
    with tempfile.TemporaryDirectory() as tmp:
        proc = _FakeProc(rows)
        client = _AppServerProcess(proc, Path(tmp))
        try:
            mapped = await asyncio.wait_for(_collect(client), timeout=2)
        finally:
            client._steer_task.cancel()
            try:
                await client._steer_task
            except asyncio.CancelledError:
                pass
        assert mapped == [], mapped


def main() -> None:
    asyncio.run(_test_completion_drains_adjacent_item())
    asyncio.run(_test_collab_agent_item_type_maps_to_normalized_name())
    print("PASS: Codex app-server completion drains adjacent final items")


if __name__ == "__main__":
    main()
