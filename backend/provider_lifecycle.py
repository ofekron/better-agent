from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar

import perf


T = TypeVar("T")


_RUNTIME_OWNER_LOOP: asyncio.AbstractEventLoop | None = None
_RUNTIME_OWNER_LOCK = threading.Lock()


def bind_runtime_owner_loop(loop: asyncio.AbstractEventLoop) -> None:
    if loop.is_closed():
        raise ValueError("provider lifecycle runtime owner loop must be open")
    global _RUNTIME_OWNER_LOOP
    with _RUNTIME_OWNER_LOCK:
        _RUNTIME_OWNER_LOOP = loop


def runtime_owner_loop(
    calling_loop: asyncio.AbstractEventLoop,
) -> asyncio.AbstractEventLoop:
    with _RUNTIME_OWNER_LOCK:
        owner = _RUNTIME_OWNER_LOOP
    return owner if owner is not None and not owner.is_closed() else calling_loop


class LifecycleOutcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    SHUTDOWN = "shutdown"


class LifecycleUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReservationToken:
    run_id: str
    generation: int
    nonce: str


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    outcome: LifecycleOutcome
    token: ReservationToken | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is LifecycleOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class MutationResult(Generic[T]):
    outcome: LifecycleOutcome
    value: T | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is LifecycleOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class BootstrapSnapshot:
    token: ReservationToken
    seed: bytes
    values: Mapping[str, Any]

    @classmethod
    def create(
        cls, token: ReservationToken, seed: bytes, values: Mapping[str, Any],
    ) -> "BootstrapSnapshot":
        return cls(token=token, seed=bytes(seed), values=_deep_freeze(values))


@dataclass(frozen=True, slots=True)
class PublishedRun(Generic[T]):
    token: ReservationToken
    value: T


@dataclass(frozen=True, slots=True)
class ShutdownInventory(Generic[T]):
    reserved: tuple[ReservationToken, ...]
    published: tuple[PublishedRun[T], ...]


@dataclass(frozen=True, slots=True)
class CancellationResult(Generic[T]):
    outcome: LifecycleOutcome
    token: ReservationToken | None = None
    value: T | None = None


@dataclass(slots=True)
class _Request(Generic[T]):
    result: concurrent.futures.Future[T]
    cancelled: bool = False
    installed: ReservationToken | None = None


def _deep_freeze(value: Any) -> Any:
    cloned = copy.deepcopy(value)
    if isinstance(cloned, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in cloned.items()})
    if isinstance(cloned, list):
        return tuple(_deep_freeze(item) for item in cloned)
    if isinstance(cloned, tuple):
        return tuple(_deep_freeze(item) for item in cloned)
    if isinstance(cloned, set):
        return frozenset(_deep_freeze(item) for item in cloned)
    return cloned


class RunLifecycleCoordinator(Generic[T]):
    """Single-owner run registry callable safely from any asyncio loop."""

    def __init__(self, owner_loop: asyncio.AbstractEventLoop) -> None:
        self._loop = owner_loop
        self._generation = 0
        self._accepting = True
        self._reservations: dict[str, ReservationToken] = {}
        self._published: dict[str, PublishedRun[T]] = {}
        self._shutdown_inventory: ShutdownInventory[T] | None = None

    @property
    def owner_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def pristine(self) -> bool:
        return (
            self._generation == 0
            and self._accepting
            and not self._reservations
            and not self._published
            and self._shutdown_inventory is None
        )

    def _submit(self, callback: Callable[[], None]) -> None:
        if self._loop.is_closed():
            raise LifecycleUnavailableError("provider lifecycle owner loop is closed")
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError as exc:
            raise LifecycleUnavailableError("provider lifecycle owner loop is unavailable") from exc

    async def _request(
        self,
        apply: Callable[[_Request[T]], T],
        rollback_cancelled: Callable[[_Request[T]], None] | None = None,
    ) -> T:
        request: _Request[T] = _Request(concurrent.futures.Future())

        def complete() -> None:
            try:
                if request.cancelled:
                    return
                request.result.set_result(apply(request))
            except BaseException as exc:
                if not request.result.done():
                    request.result.set_exception(exc)

        self._submit(complete)
        wrapped = asyncio.wrap_future(request.result)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            acknowledgement: concurrent.futures.Future[None] = concurrent.futures.Future()

            def cancel_on_owner() -> None:
                try:
                    request.cancelled = True
                    if rollback_cancelled is not None:
                        rollback_cancelled(request)
                    acknowledgement.set_result(None)
                except BaseException as exc:
                    acknowledgement.set_exception(exc)

            try:
                self._submit(cancel_on_owner)
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await asyncio.shield(asyncio.wrap_future(acknowledgement))
            finally:
                raise

    async def admit(self, run_id: str, *, nonce: str | None = None) -> AdmissionResult:
        if not run_id:
            raise ValueError("run_id must be non-empty")
        if nonce is not None and (not isinstance(nonce, str) or not nonce.strip()):
            raise ValueError("nonce must be a non-empty string")
        started = time.perf_counter()

        def apply(request: _Request[AdmissionResult]) -> AdmissionResult:
            if not self._accepting:
                return AdmissionResult(LifecycleOutcome.SHUTDOWN)
            if run_id in self._reservations or run_id in self._published:
                return AdmissionResult(LifecycleOutcome.DUPLICATE)
            self._generation += 1
            token = ReservationToken(run_id, self._generation, nonce or uuid.uuid4().hex)
            request.installed = token
            self._reservations[run_id] = token
            return AdmissionResult(LifecycleOutcome.ACCEPTED, token)

        def rollback(request: _Request[AdmissionResult]) -> None:
            token = request.installed
            if token is not None and self._reservations.get(run_id) == token:
                self._reservations.pop(run_id)

        try:
            result = await self._request(apply, rollback)
            perf.record_count("provider.lifecycle.admission", 1)
            perf.record_count(f"provider.lifecycle.outcome.{result.outcome.value}", 1)
            return result
        finally:
            perf.record("provider.lifecycle.admission.e2e", (time.perf_counter() - started) * 1000)

    async def publish(self, token: ReservationToken, value: T) -> MutationResult[T]:
        def apply(_: _Request[MutationResult[T]]) -> MutationResult[T]:
            if self._reservations.get(token.run_id) != token:
                return MutationResult(LifecycleOutcome.STALE)
            self._reservations.pop(token.run_id)
            self._published[token.run_id] = PublishedRun(token, value)
            return MutationResult(LifecycleOutcome.ACCEPTED, value)

        return await self._request(apply)

    async def quiesce(self) -> None:
        await self._request(lambda _: setattr(self, "_accepting", False))

    async def rollback(self, token: ReservationToken) -> MutationResult[ReservationToken]:
        def apply(_: _Request[MutationResult[ReservationToken]]) -> MutationResult[ReservationToken]:
            if self._reservations.get(token.run_id) != token:
                return MutationResult(LifecycleOutcome.STALE)
            self._reservations.pop(token.run_id)
            return MutationResult(LifecycleOutcome.ACCEPTED, token)

        return await self._request(apply)

    async def retire(self, token: ReservationToken, value: T) -> MutationResult[T]:
        def apply(_: _Request[MutationResult[T]]) -> MutationResult[T]:
            installed = self._published.get(token.run_id)
            if installed is None or installed.token != token or installed.value != value:
                return MutationResult(LifecycleOutcome.STALE)
            self._published.pop(token.run_id)
            return MutationResult(LifecycleOutcome.ACCEPTED, value)

        return await self._request(apply)

    async def cancel(self, run_id: str) -> CancellationResult[T]:
        def apply(_: _Request[CancellationResult[T]]) -> CancellationResult[T]:
            token = self._reservations.pop(run_id, None)
            if token is not None:
                return CancellationResult(LifecycleOutcome.ACCEPTED, token)
            installed = self._published.pop(run_id, None)
            if installed is not None:
                return CancellationResult(
                    LifecycleOutcome.ACCEPTED, installed.token, installed.value
                )
            return CancellationResult(LifecycleOutcome.STALE)

        return await self._request(apply)

    async def get(self, run_id: str) -> T | None:
        return await self._request(
            lambda _: self._published.get(run_id).value if run_id in self._published else None
        )

    async def snapshot(self) -> tuple[PublishedRun[T], ...]:
        return await self._request(lambda _: tuple(self._published.values()))

    async def shutdown(self) -> ShutdownInventory[T]:
        def apply(_: _Request[ShutdownInventory[T]]) -> ShutdownInventory[T]:
            self._accepting = False
            if self._shutdown_inventory is None:
                self._shutdown_inventory = ShutdownInventory(
                    tuple(self._reservations.values()), tuple(self._published.values())
                )
                self._reservations.clear()
                self._published.clear()
            return self._shutdown_inventory

        return await self._request(apply)


def ensure_runtime_owner(
    lifecycle: RunLifecycleCoordinator[T] | None,
    calling_loop: asyncio.AbstractEventLoop,
) -> RunLifecycleCoordinator[T]:
    owner = runtime_owner_loop(calling_loop)
    if lifecycle is None:
        return RunLifecycleCoordinator(owner)
    if lifecycle.owner_loop is owner:
        return lifecycle
    if lifecycle.owner_loop.is_closed() or lifecycle.pristine:
        return RunLifecycleCoordinator(owner)
    raise LifecycleUnavailableError(
        "active provider lifecycle belongs to a different owner loop"
    )
