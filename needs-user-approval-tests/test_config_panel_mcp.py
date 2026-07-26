"""`open-config-panel` MCP server against real agents.

This tool deliberately mutates no session state — the internal route is inline
and returns the panel to the caller. The only backend-owned proof it ran is
the persisted `tool_use` block on the assistant message, which is structured
JSON written by the ingestion funnel, not model prose.
"""
from __future__ import annotations

import _live_agent
from _live_agent import Case, require_cli, tool_calls

SERVER = "open-config-panel"
TOOL = "open_config_panel"
VENDORS = _live_agent.vendors_for_server(SERVER)


def _prompt() -> str:
    return (
        "This is an automated integration test of Better Agent tool injection. "
        "Call the MCP tool named open_config_panel from the 'open-config-panel' "
        "server exactly once. Do not call any other tool. After the tool "
        "returns, reply with the single word: done"
    )


async def _open_config_panel(vendor, backend, cwd):
    require_cli(vendor)

    sid = backend.new_session(vendor, f"config-panel/{vendor.kind}", str(cwd))
    await backend.run_turn(vendor, sid=sid, prompt=_prompt(), cwd=str(cwd))

    calls = tool_calls(sid, [TOOL])
    if not calls:
        raise AssertionError(
            f"no persisted {TOOL} tool_use block on session {sid} — the tool was "
            "never invoked or the event never reached the render tree"
        )


def cases() -> list[Case]:
    return [Case(SERVER, TOOL, vendor, _open_config_panel) for vendor in VENDORS]


__all__ = ["cases"]
