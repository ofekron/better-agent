"""Bundled-extension MCP servers against real agents.

These servers are contributed by the extensions the installer ships, so they
only exist once the backend's startup reconcile has installed them. Each case
asks the running backend what it actually assembled and skips with a precise
reason when the server is absent, rather than failing as if the model had
misbehaved.

* `better-agent-coordination.lock_ops` — writes `coordination/locks.json` under
  the isolated home; the acquired key is the proof.
* `better-agent-session-bridge.search_sessions` — read-only, so the proof is
  the persisted `tool_use` block, same as `open_config_panel`.
"""
from __future__ import annotations

import _live_agent
from _live_agent import Case, Skip, require_cli, tool_calls

COORDINATION_SERVER = "better-agent-coordination"
SESSION_BRIDGE_SERVER = "better-agent-session-bridge"


def _require_server(backend, vendor, server: str) -> None:
    available = backend.assembled_servers(vendor.kind)
    if server not in available:
        raise Skip(
            f"{server} is not assembled for {vendor.kind}; "
            f"available={sorted(available)}"
        )


def _lock_prompt(key: str) -> str:
    return (
        "This is an automated integration test. Call the lock_ops tool exactly "
        f"once with keys=[{key!r}] and timeout_seconds=10 to acquire the lock. "
        "Do not release it. Do not call any other tool. Then reply with the "
        "single word: done"
    )


async def _lock_ops(vendor, backend, cwd):
    require_cli(vendor)
    import coordination

    _require_server(backend, vendor, COORDINATION_SERVER)

    key = f"file_edit:/live-mcp-probe/{vendor.kind}"
    sid = backend.new_session(vendor, f"coordination/{vendor.kind}", str(cwd))
    token = ""
    try:
        await backend.run_turn(
            vendor, sid=sid, prompt=_lock_prompt(key), cwd=str(cwd)
        )
        record = _lock_record(key)
        if record is None:
            raise AssertionError(
                f"{key!r} is absent from the lock store — lock_ops never acquired it"
            )
        token = str(record.get("holder_token") or "")
    finally:
        if token:
            await coordination.lock_ops(key=key, release=True, holder_token=token)


def _lock_record(key: str) -> dict | None:
    """The persisted lock row, read straight from the disk store the tool owns."""
    import json

    import coordination

    path = coordination._lock_store_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    record = (data.get("locks") or {}).get(key)
    return record if isinstance(record, dict) else None


def _search_prompt() -> str:
    return (
        "This is an automated integration test. Call the search_sessions tool "
        "exactly once with query='integration probe'. Do not call any other "
        "tool. Then reply with the single word: done"
    )


async def _search_sessions(vendor, backend, cwd):
    require_cli(vendor)
    _require_server(backend, vendor, SESSION_BRIDGE_SERVER)

    sid = backend.new_session(vendor, f"session-bridge/{vendor.kind}", str(cwd))
    await backend.run_turn(vendor, sid=sid, prompt=_search_prompt(), cwd=str(cwd))

    if not tool_calls(sid, ["search_sessions"]):
        raise AssertionError(
            f"no persisted search_sessions tool_use block on session {sid}"
        )


def cases() -> list[Case]:
    out: list[Case] = []
    for vendor in _live_agent.VENDORS:
        if not _live_agent.builtin_servers_for(vendor.kind):
            # No built-in assembler call means no extension servers either.
            continue
        out.append(Case(COORDINATION_SERVER, "lock_ops", vendor, _lock_ops))
        out.append(
            Case(SESSION_BRIDGE_SERVER, "search_sessions", vendor, _search_sessions)
        )
    return out


__all__ = ["cases"]
