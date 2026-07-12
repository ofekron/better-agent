from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

import httpx

import runtime_endpoints
from bff_runtime_service import read_service_token


class RuntimeUpstreamUnavailable(RuntimeError):
    pass


@dataclass
class _Generation:
    descriptor: dict
    service_token: str
    client: httpx.AsyncClient
    leases: int = 0
    retired: bool = False


class RuntimeUpstreamLease:
    def __init__(self, owner: "RuntimeUpstream", generation: _Generation) -> None:
        self._owner = owner
        self._generation = generation
        self._released = False

    @property
    def client(self) -> httpx.AsyncClient:
        return self._generation.client

    @property
    def descriptor(self) -> dict:
        return dict(self._generation.descriptor)

    @property
    def service_token(self) -> str:
        return self._generation.service_token

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._owner.release(self._generation)


class RuntimeUpstream:
    def __init__(
        self,
        *,
        descriptor_reader: Callable[[], dict] = runtime_endpoints.read_app_endpoint,
        token_reader: Callable[[], str] = read_service_token,
        client_factory: Callable[[dict], httpx.AsyncClient] | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._current: _Generation | None = None
        self._descriptor_reader = descriptor_reader
        self._token_reader = token_reader
        self._client_factory = client_factory or self._new_client

    @staticmethod
    def _new_client(descriptor: dict) -> httpx.AsyncClient:
        if descriptor["kind"] == "uds":
            transport = httpx.AsyncHTTPTransport(uds=descriptor["path"])
            base_url = "http://better-agent-runtime"
        else:
            transport = None
            base_url = f"http://{descriptor['host']}:{descriptor['port']}"
        return httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            timeout=httpx.Timeout(300.0, connect=10.0),
        )

    async def acquire(self) -> RuntimeUpstreamLease:
        try:
            descriptor, service_token = await asyncio.gather(
                asyncio.to_thread(self._descriptor_reader),
                asyncio.to_thread(self._token_reader),
            )
        except (RuntimeError, OSError) as exc:
            raise RuntimeUpstreamUnavailable("runtime unavailable") from exc
        close: httpx.AsyncClient | None = None
        async with self._lock:
            current = self._current
            if (
                current is None
                or current.descriptor != descriptor
                or current.service_token != service_token
            ):
                replacement = _Generation(
                    descriptor=dict(descriptor),
                    service_token=service_token,
                    client=self._client_factory(descriptor),
                )
                self._current = replacement
                if current is not None:
                    current.retired = True
                    if current.leases == 0:
                        close = current.client
                current = replacement
            current.leases += 1
            lease = RuntimeUpstreamLease(self, current)
        if close is not None:
            await close.aclose()
        return lease

    async def release(self, generation: _Generation) -> None:
        close: httpx.AsyncClient | None = None
        async with self._lock:
            if generation.leases <= 0:
                return
            generation.leases -= 1
            if generation.retired and generation.leases == 0:
                close = generation.client
        if close is not None:
            await close.aclose()

    async def shutdown(self) -> None:
        close: httpx.AsyncClient | None = None
        async with self._lock:
            current = self._current
            self._current = None
            if current is not None:
                current.retired = True
                if current.leases == 0:
                    close = current.client
        if close is not None:
            await close.aclose()


runtime_upstream = RuntimeUpstream()
