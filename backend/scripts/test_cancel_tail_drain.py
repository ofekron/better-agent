"""Cancel-tail drain regression test.

After an interrupt, the runner keeps draining the CLI's wind-down for
up to 15s while the CLI emits its tail (tool aborts + ResultMessage).
`_drive_cli_run`'s cancel branch must consume that ENTIRE tail onto
the cancelled turn — appending to its events AND emitting via
ws_callback — instead of grabbing at most one event and bailing.
Bailing leaves the tail to the backup tailer's orphan ingest, which
later seq-brackets it onto the NEXT turn's message (the
interleaved-turns bug).

Run with:
    cd backend && .venv/bin/python scripts/test_cancel_tail_drain.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_drain_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from turn_manager import TurnManager  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


class _Event:
    def __init__(self, type_: str, data: dict) -> None:
        self.type = type_
        self.data = data


class _StubCoordinator:
    def __init__(self) -> None:
        self._in_flight_prompts: dict[str, int] = {}
        self._prompt_queues: dict = {}
        self._session_cancelled: dict[str, bool] = {}

    @staticmethod
    def _cancel_turn_fanout(run_id: str) -> bool:
        return True


class _TailProvider:
    """Emits one streaming event, then — only after cancel_turn — the
    interrupted CLI's tail: two tool-abort events and a complete."""

    def __init__(self) -> None:
        self._runs: dict = {}
        self.queue: asyncio.Queue = None
        self.cancelled = False
        self._alive = True

    def start_run(self, **kw) -> None:
        self.queue = kw["queue"]
        self.queue.put_nowait(_Event("agent_message", {"uuid": "ev-live-1"}))

    def is_running(self, run_id: str) -> bool:
        return self._alive

    def cancel_turn(self, run_id: str) -> bool:
        self.cancelled = True

        async def _emit_tail() -> None:
            await asyncio.sleep(0.2)
            self.queue.put_nowait(_Event("agent_message", {"uuid": "ev-tail-1"}))
            await asyncio.sleep(0.2)
            self.queue.put_nowait(_Event("agent_message", {"uuid": "ev-tail-2"}))
            await asyncio.sleep(0.2)
            self.queue.put_nowait(_Event("complete", {"success": False}))
            self._alive = False

        asyncio.get_running_loop().create_task(_emit_tail())
        return True


def main() -> int:
    c = _StubCoordinator()
    tm = TurnManager(c)
    provider = _TailProvider()
    c.provider_for_session = lambda sid: provider

    class _UPM:
        @staticmethod
        def get_in_flight_lifecycle_msg_id(sid):
            return None

    c.user_prompt_manager = _UPM()

    seen_ws: list[dict] = []

    async def _ws(e: dict) -> None:
        seen_ws.append(e)

    async def _go() -> dict:
        ev = asyncio.Event()

        async def _cancel_soon() -> None:
            await asyncio.sleep(0.5)
            ev.set()

        asyncio.get_running_loop().create_task(_cancel_soon())
        return await tm._drive_cli_run(
            prompt="p",
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id="sid-drain",
            cancel_event=ev,
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-drain",
        )

    result = asyncio.run(_go())

    print("cancel-tail drain onto the cancelled turn")
    check("provider cancel was requested", provider.cancelled)
    ev_uuids = [
        (e.get("data") or {}).get("uuid")
        for e in result.get("events", [])
        if e.get("type") == "agent_message"
    ]
    check("tail event 1 on cancelled turn", "ev-tail-1" in ev_uuids)
    check("tail event 2 on cancelled turn", "ev-tail-2" in ev_uuids)
    check(
        "terminal complete drained",
        any(e.get("type") == "complete" for e in result.get("events", [])),
    )
    ws_uuids = [
        (e.get("data") or {}).get("uuid")
        for e in seen_ws
        if e.get("type") == "agent_message"
    ]
    check("tail events reached ws_callback (render tree)",
          "ev-tail-1" in ws_uuids and "ev-tail-2" in ws_uuids)
    check("turn reports cancelled", result.get("success") is False)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
