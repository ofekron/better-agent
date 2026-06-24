"""Manager delegation behavior.

Manager is no longer a distinct orchestration strategy — it is the
delegation-capable behavior layered on the single strategy. Turn
handling goes through `orchs.native.handle_turn`; the manager BOOTSTRAP
prompt is injected by `NativeStrategy.wrap_cli_prompt` when the session's
`orchestration_mode == "manager"`, and the delegate MCP tool is gated in
runner.py.

Public surface (called from main.py / runner.py):

  - `run_delegation(coordinator, ...)` — entry point for the in-process
    delegate MCP tool handler in runner.py.
  - `resolve_approval(coordinator, delegation_id, status)` — REST
    handler hook in main.py for approve/deny on fresh-worker creation.

Internals live in submodules:

  - `bootstrap` — BOOTSTRAP_PROMPT + per-turn prompt wrapping.
  - `_delegation` — run_delegation + per-pair lock + locked inner body.
  - `_approval` — fresh-worker approval handshake + spawn/init.
  - `_rewind` — worker fan-out rewind for `rewind_files` on a manager.

Persistence stores used by delegation (worker registry + pending
approvals) live in `backend/stores/` — they are NOT manager-only
(supervisor mode + future modes can also create workers and
approvals), so they shouldn't live under this package.
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "run_delegation":
        from orchs.manager._delegation import run_delegation
        return run_delegation
    if name == "resolve_approval":
        from orchs.manager._approval import resolve_approval
        return resolve_approval
    raise AttributeError(f"module 'orchs.manager' has no attribute {name!r}")


__all__ = ["run_delegation", "resolve_approval"]
