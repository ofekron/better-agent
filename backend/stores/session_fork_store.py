"""Session fork mapping storage.

This module is the neutral API for per-(caller, session) delegate forks.
It currently reuses the fork-map section of worker_store's on-disk schema.
"""

from __future__ import annotations

from typing import Optional

from stores import worker_store


def get_fork_record(
    cwd: str,
    caller_agent_session_id: str,
    session_agent_session_id: str,
) -> Optional[dict]:
    return worker_store.get_fork_record(
        cwd, caller_agent_session_id, session_agent_session_id,
    )


def set_fork(
    cwd: str,
    caller_agent_session_id: str,
    session_agent_session_id: str,
    fork_agent_session_id: str,
) -> None:
    worker_store.set_fork(
        cwd, caller_agent_session_id, session_agent_session_id, fork_agent_session_id,
    )


def touch_fork(
    cwd: str,
    caller_agent_session_id: str,
    session_agent_session_id: str,
) -> None:
    worker_store.touch_fork(cwd, caller_agent_session_id, session_agent_session_id)


def clear_fork(
    cwd: str,
    caller_agent_session_id: str,
    session_agent_session_id: str,
) -> bool:
    return worker_store.clear_fork(cwd, caller_agent_session_id, session_agent_session_id)


def clear_forks_for_session_everywhere(session_agent_session_id: str) -> list[str]:
    return worker_store.clear_forks_for_worker_everywhere(session_agent_session_id)


def clear_forks_for_caller_everywhere(caller_agent_session_id: str) -> list[str]:
    return worker_store.clear_forks_for_caller_everywhere(caller_agent_session_id)
