#!/usr/bin/env python3

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import _test_home
_test_home.isolate("bc-timer-tools-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provider_claude import ClaudeProvider, TIMER_TOOLS


class _Popen:
    pid = 123

    def poll(self):
        return None


def main() -> int:
    provider = ClaudeProvider({"id": "timer-tools-test"})
    run_id = "timer-tools-run"
    loop = asyncio.new_event_loop()

    with (
        patch("provider_claude.containment", create=True),
        patch("provider_claude.subprocess.Popen", return_value=_Popen()),
        patch.object(provider, "_bootstrap_run"),
        patch.object(provider, "_write_backend_state"),
        patch("containment.containment") as containment,
    ):
        containment.return_value.create.return_value = None
        provider.start_run(
            run_id=run_id,
            prompt="test",
            cwd=str(Path.cwd()),
            loop=loop,
            queue=asyncio.Queue(),
            model=None,
            reasoning_effort=None,
            session_id=None,
            mode="native",
            app_session_id="session",
            disallowed_tools=["AskUserQuestion"],
        )

    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
    loop.close()

    payload = json.loads(
        (Path(os.environ["BETTER_CLAUDE_HOME"]) / "runs" / run_id / "input.json")
        .read_text(encoding="utf-8")
    )
    missing = [tool for tool in TIMER_TOOLS if tool not in payload["disallowed_tools"]]
    if missing:
        print(f"FAIL missing timer tools: {missing}")
        return 1
    print("PASS provider always writes timer tools to input.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
