"""`capabilities` MCP server against real agents.

`load_capability` writes through to `session["active_capability_ids"]`, which
is the assertion target. The catalog is built from active extensions'
`entrypoints.capabilities`, and a fresh isolated home has none, so each case
seeds one synthetic descriptor for the duration of the run. The backend serving
the loopback call is this process, so seeding here is genuinely what the agent
sees — not a stub in front of the tool.

`release_capability` is asserted in the same turn: the id must be gone again
once the agent releases it.
"""
from __future__ import annotations

import contextlib

import _live_agent
from _live_agent import Case, require_cli

SERVER = "capabilities"
VENDORS = _live_agent.vendors_for_server(SERVER)

CAPABILITY_EXTENSION = "better-agent.live-mcp-probe"
CAPABILITY_ID = "probe"
FULL_CAPABILITY_ID = f"{CAPABILITY_EXTENSION}:{CAPABILITY_ID}"


@contextlib.contextmanager
def _seeded_capability():
    """Publish one capability into the live catalog for this case only."""
    import extension_store

    record = {
        "manifest": {
            "id": CAPABILITY_EXTENSION,
            "entrypoints": {
                "capabilities": [
                    {"id": CAPABILITY_ID, "scope": "session", "skill": []},
                ]
            },
        }
    }
    original = extension_store._active_records
    extension_store._active_records = lambda: [*original(), record]
    try:
        yield FULL_CAPABILITY_ID
    finally:
        extension_store._active_records = original


def _load_prompt(capability_id: str) -> str:
    return (
        "This is an automated integration test. Using the 'capabilities' MCP "
        "server, call list_capabilities once, then call load_capability once "
        f"with id={capability_id!r}. Do not release it. Do not call any other "
        "tool. Then reply with the single word: done"
    )


def _release_prompt(capability_id: str) -> str:
    return (
        "This is an automated integration test. Using the 'capabilities' MCP "
        f"server, call release_capability once with id={capability_id!r}. Do "
        "not call any other tool. Then reply with the single word: done"
    )


async def _load_and_release(vendor, backend, cwd):
    require_cli(vendor)
    from session_manager import manager as session_manager

    with _seeded_capability() as capability_id:
        sid = backend.new_session(vendor, f"capabilities/{vendor.kind}", str(cwd))

        await backend.run_turn(
            vendor, sid=sid, prompt=_load_prompt(capability_id), cwd=str(cwd)
        )
        active = (session_manager.get(sid) or {}).get("active_capability_ids") or []
        if capability_id not in active:
            raise AssertionError(
                f"load_capability did not activate {capability_id}; active={active}"
            )

        await backend.run_turn(
            vendor, sid=sid, prompt=_release_prompt(capability_id), cwd=str(cwd)
        )
        active = (session_manager.get(sid) or {}).get("active_capability_ids") or []
        if capability_id in active:
            raise AssertionError(
                f"release_capability left {capability_id} active; active={active}"
            )


def cases() -> list[Case]:
    return [
        Case(SERVER, "load_capability+release_capability", vendor, _load_and_release)
        for vendor in VENDORS
    ]


__all__ = ["cases"]
