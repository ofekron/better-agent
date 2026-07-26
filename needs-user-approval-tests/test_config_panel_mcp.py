"""`open-config-panel` MCP server against real agents.

This tool deliberately mutates no session state — the internal route is inline
and returns the panel to the caller. The only backend-owned proof it ran is
the persisted `tool_use` block on the assistant message, which is structured
JSON written by the ingestion funnel, not model prose.
"""
from __future__ import annotations

import _live_agent
from _live_agent import Case, observed_tools, require_cli, tool_calls, tool_prompt

SERVER = "open-config-panel"
TOOL = "open_config_panel"
VENDORS = _live_agent.vendors_for_server(SERVER)


def _prompt() -> str:
    return tool_prompt(SERVER, TOOL, "It takes no required arguments.")


async def _open_config_panel(vendor, backend, cwd):
    require_cli(vendor)

    sid = backend.new_session(vendor, f"config-panel/{vendor.kind}", str(cwd))
    turn = await backend.run_turn(vendor, sid=sid, prompt=_prompt(), cwd=str(cwd))

    if not tool_calls(turn.events, [TOOL]):
        raise AssertionError(
            f"the turn emitted no {TOOL} tool_use block; the agent called "
            f"{observed_tools(turn.events)}"
        )


def cases() -> list[Case]:
    return [Case(SERVER, TOOL, vendor, _open_config_panel) for vendor in VENDORS]


__all__ = ["cases"]
