from __future__ import annotations

import asyncio
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _SessionManager:
    def __init__(self) -> None:
        self.persisted: list[str] = []

    def set_agent_sid(self, _session_id, _mode, sid):
        self.persisted.append(sid)

    def get(self, _session_id):
        return {}


sessions = _SessionManager()
session_module = type(sys)("session_manager")
session_module.manager = sessions
turn_module = type(sys)("turn_manager")
turn_module._release_abandoned_queue = lambda *_args, **_kwargs: None
sys.modules["session_manager"] = session_module
sys.modules["turn_manager"] = turn_module

from orchs._subprocess_agent import SubprocessAgent
from provider import StreamEvent


class _Provider:
    def __init__(self, event: StreamEvent) -> None:
        self.event = event

    def start_run(self, **kwargs) -> None:
        kwargs["loop"].call_soon_threadsafe(
            kwargs["queue"].put_nowait,
            self.event,
        )


class _Coordinator:
    internal_token = "token"

    class turn_manager:
        current_assistant_msgs = {}

    def __init__(self, event: StreamEvent) -> None:
        self.provider = _Provider(event)

    def provider_for_session(self, _session_id):
        return self.provider

    async def persist_and_dispatch_raw(self, _session_id, _event) -> None:
        return None


async def _assert_terminal_error(event: StreamEvent, expected: str) -> None:
    agent = SubprocessAgent(agent_session_id="base", cwd="/repo")
    try:
        await agent.init(
            _Coordinator(event),
            model="model",
            prep_prompt="prepare",
            cancel_event=asyncio.Event(),
        )
    except RuntimeError as exc:
        assert str(exc) == expected
    else:
        raise AssertionError("provider initialization failure did not propagate")
    assert sessions.persisted == []


async def _main() -> None:
    await _assert_terminal_error(
        StreamEvent("error", {"error": "provider transport exploded"}),
        "provider transport exploded",
    )
    await _assert_terminal_error(
        StreamEvent("complete", {
            "success": False,
            "error": "provider rejected configuration",
            "session_id": None,
        }),
        "provider rejected configuration",
    )


def main() -> int:
    asyncio.run(_main())
    print("PASS: subprocess init preserves provider terminal errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
