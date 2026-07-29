from __future__ import annotations

import asyncio
import copy
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


ListTools = Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]]


def _config_fingerprint(config: Mapping[str, Any], key: bytes) -> str:
    encoded = json.dumps(
        config,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hmac.digest(key, encoded, "sha256").hex()


class McpToolDiscovery:
    def __init__(self) -> None:
        self._fingerprint_key = secrets.token_bytes(32)
        self._cache: dict[
            tuple[str, str, ListTools],
            tuple[dict[str, Any], ...],
        ] = {}
        self._fingerprints: dict[tuple[str, ListTools], str] = {}
        self._inflight: dict[
            tuple[str, str, ListTools],
            asyncio.Task[tuple[dict[str, Any], ...]],
        ] = {}

    def invalidate(self, server_name: str | None = None) -> None:
        if server_name is None:
            self._cache.clear()
            self._fingerprints.clear()
            return
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if key[0] != server_name
        }
        self._fingerprints = {
            key: value
            for key, value in self._fingerprints.items()
            if key[0] != server_name
        }

    async def discover(
        self,
        server_name: str,
        config: dict[str, Any],
        list_tools: ListTools,
    ) -> list[dict[str, Any]]:
        fingerprint = _config_fingerprint(config, self._fingerprint_key)
        server_key = (server_name, list_tools)
        previous = self._fingerprints.get(server_key)
        if previous is not None and previous != fingerprint:
            self._cache.pop((server_name, previous, list_tools), None)
        self._fingerprints[server_key] = fingerprint
        key = (server_name, fingerprint, list_tools)
        cached = self._cache.get(key)
        if cached is not None:
            return copy.deepcopy(list(cached))
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._load(server_key, config, list_tools, key),
            )
            self._inflight[key] = task
            task.add_done_callback(
                lambda completed, *, cache_key=key: self._clear_inflight(
                    cache_key,
                    completed,
                ),
            )
        try:
            tools = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        return copy.deepcopy(list(tools))

    def _clear_inflight(
        self,
        key: tuple[str, str, ListTools],
        completed: asyncio.Task[tuple[dict[str, Any], ...]],
    ) -> None:
        if self._inflight.get(key) is completed:
            self._inflight.pop(key, None)
        if not completed.cancelled():
            completed.exception()

    async def _load(
        self,
        server_key: tuple[str, ListTools],
        config: dict[str, Any],
        list_tools: ListTools,
        key: tuple[str, str, ListTools],
    ) -> tuple[dict[str, Any], ...]:
        server_name = server_key[0]
        tools = await list_tools(server_name, copy.deepcopy(config))
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict)
            for tool in tools
        ):
            raise RuntimeError(
                f"extension MCP {server_name!r} returned an invalid tools/list result",
            )
        authoritative = tuple(copy.deepcopy(tools))
        if authoritative and self._fingerprints.get(server_key) == key[1]:
            self._cache[key] = authoritative
        return authoritative


tool_discovery = McpToolDiscovery()
