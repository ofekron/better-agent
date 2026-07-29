"""Shared session-lookup helpers used across route modules.

Thin wrappers around `session_manager` — no route-specific state, so
every domain module (providers, sessions, workers, ...) imports these
directly instead of routing through a per-module `configure()`.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

from fastapi import HTTPException

from i18n import t
from session_manager import manager as session_manager


def require_session(session_id: str) -> dict:
    """Fetch session by id or raise 404 with the standard
    `session_not_found_retry` detail. Replaces the 2-line guard
    duplicated across every route that mutates a session by id.

    Returns a `get_lite()` snapshot — caller MUST NOT read
    `msg.events` / `msg.workers[*].events`
    from the returned dict (they will be empty lists). Callers that
    need events should call `session_manager.get(sid)` explicitly."""
    session = session_manager.get_lite(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return session


async def session_exists(session_id: str) -> bool:
    return await asyncio.to_thread(session_manager.exists, session_id)


async def session_lite(session_id: str) -> dict | None:
    return await asyncio.to_thread(session_manager.get_lite, session_id)


def existing_session_ids(session_ids: Iterable[str]) -> set[str]:
    return {sid for sid in session_ids if session_manager.exists(sid)}


def session_lite_by_id(session_ids: Iterable[str]) -> dict[str, dict | None]:
    return {sid: session_manager.get_lite(sid) for sid in session_ids}


async def existing_session_ids_async(session_ids: Iterable[str]) -> set[str]:
    return await asyncio.to_thread(existing_session_ids, set(session_ids))


async def session_lite_by_id_async(
    session_ids: Iterable[str],
) -> dict[str, dict | None]:
    return await asyncio.to_thread(session_lite_by_id, set(session_ids))


async def require_session_async(session_id: str) -> dict:
    session = await session_lite(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=t("error.session_not_found_retry"),
        )
    return session
