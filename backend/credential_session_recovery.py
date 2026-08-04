from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from credential_session_client import CredentialSessionRestartRequired


T = TypeVar("T")


async def _request_restart() -> bool:
    from ops_api import request_supervised_backend_restart

    return await request_supervised_backend_restart()


async def resolve_provider_with_restart(
    resolve: Callable[..., T],
    *args: object,
) -> T:
    try:
        return await asyncio.to_thread(resolve, *args)
    except CredentialSessionRestartRequired:
        await _request_restart()
        raise
