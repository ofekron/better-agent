from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Awaitable, Callable, Mapping

import config_store
from model_catalog_refresh_engine import CatalogRefreshEngine
from model_catalog_refresh_state import CatalogChangedFact, CatalogProjection
from model_catalog_source_watcher import CatalogSourceWatcher
from model_catalog_watch_spec import build_source_watch_spec


logger = logging.getLogger(__name__)
FactSink = Callable[[CatalogChangedFact], Awaitable[None] | None]
WatcherFactory = Callable[[], CatalogSourceWatcher]
CATALOG_MAX_AGE_SECONDS = 24 * 60 * 60
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 5 * 60.0


class CatalogRefreshError(RuntimeError):
    pass


class CatalogRefreshOwner:
    def __init__(
        self,
        *,
        fact_sink: FactSink | None = None,
        max_age_seconds: float = CATALOG_MAX_AGE_SECONDS,
        retry_base_seconds: float = RETRY_BASE_SECONDS,
        retry_max_seconds: float = RETRY_MAX_SECONDS,
        max_concurrency: int = 4,
        watcher_factory: WatcherFactory = CatalogSourceWatcher,
    ) -> None:
        if (
            max_age_seconds <= 0
            or retry_base_seconds <= 0
            or retry_max_seconds < retry_base_seconds
            or type(max_concurrency) is not int
            or max_concurrency <= 0
        ):
            raise CatalogRefreshError("invalid refresh owner configuration")
        self._wake = asyncio.Event()
        self._dirty_event = asyncio.Event()
        self._engine = CatalogRefreshEngine(
            wake=self._wake.set,
            fact_sink=fact_sink,
            max_age_seconds=float(max_age_seconds),
            retry_base_seconds=float(retry_base_seconds),
            retry_max_seconds=float(retry_max_seconds),
            max_concurrency=max_concurrency,
        )
        self._ready = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None
        self._watcher_factory = watcher_factory
        self._watcher: CatalogSourceWatcher | None = None
        self._starting_watcher: CatalogSourceWatcher | None = None
        self._watcher_guard: asyncio.Task[None] | None = None
        self._watch_spec_fingerprint = ""
        self._watcher_failure = False
        self._watcher_failures = 0
        self._watcher_retry_at: float | None = None
        self._retry_base_seconds = float(retry_base_seconds)
        self._retry_max_seconds = float(retry_max_seconds)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._notification_lock = threading.Lock()
        self._dirty = False
        self._closed = True

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def snapshot(self, provider_id: str) -> CatalogProjection | None:
        return self._engine.snapshot(provider_id)

    async def start(self) -> None:
        if self.running:
            return
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._dirty = True
        self._ready = asyncio.Event()
        self._wake = asyncio.Event()
        self._dirty_event = asyncio.Event()
        self._engine.bind_wake(self._wake.set)
        self._run_task = asyncio.create_task(
            self._run(),
            name="model-catalog-refresh-owner",
        )

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def shutdown(self) -> None:
        self._closed = True
        run_task = self._run_task
        if run_task is not None:
            run_task.cancel()
        await self._engine.shutdown()
        if run_task is not None:
            await asyncio.gather(run_task, return_exceptions=True)
        await self._stop_watcher()
        self._run_task = None
        self._loop = None

    def notify_provider_state_changed(self) -> None:
        self._notify_threadsafe()

    def notify_provider_auth_changed(self, _provider_id: str) -> None:
        self._notify_threadsafe()

    async def refresh(self, provider_id: str) -> CatalogProjection:
        if type(provider_id) is not str or not provider_id:
            raise CatalogRefreshError("invalid provider id")
        await self.wait_ready()
        try:
            projection = await self._engine.manual_refresh(provider_id)
        except RuntimeError as exc:
            raise CatalogRefreshError(str(exc)) from exc
        if self._watcher_failure:
            await self._engine.degrade_watcher()
            projection = self._engine.snapshot(provider_id)
            if projection is None:
                raise CatalogRefreshError("catalog projection unavailable")
        return projection

    def _notify_threadsafe(self) -> None:
        with self._notification_lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
        loop.call_soon_threadsafe(self._mark_dirty)

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._dirty_event.set()
        self._wake.set()

    async def _run(self) -> None:
        try:
            await self._reconcile_until_quiet()
            self._ready.set()
            while not self._closed:
                timeout = self._next_timeout()
                timed_out = False
                try:
                    if timeout is None:
                        await self._wake.wait()
                    else:
                        await asyncio.wait_for(self._wake.wait(), timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                self._wake.clear()
                if not self._dirty and not timed_out:
                    continue
                await self._reconcile_until_quiet()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("model catalog refresh owner failed")
            self._ready.set()

    async def _reconcile_until_quiet(self) -> None:
        while not self._closed:
            self._dirty = False
            self._wake.clear()
            self._dirty_event.clear()
            reconcile = asyncio.create_task(
                self._reconcile(),
                name="model-catalog-state-reconcile",
            )
            changed = asyncio.create_task(
                self._dirty_event.wait(),
                name="model-catalog-reconcile-wake",
            )
            try:
                done, _pending = await asyncio.wait(
                    (reconcile, changed),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if changed in done and self._dirty and not reconcile.done():
                    reconcile.cancel()
                    await asyncio.gather(reconcile, return_exceptions=True)
                    continue
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
                state = await reconcile
            except BaseException:
                reconcile.cancel()
                changed.cancel()
                await asyncio.gather(
                    reconcile,
                    changed,
                    return_exceptions=True,
                )
                raise
            await self._ensure_watcher(state)
            if not self._dirty:
                return

    async def _reconcile(self) -> dict:
        state = await asyncio.to_thread(config_store.list_providers)
        await self._engine.reconcile(state)
        return state

    async def _ensure_watcher(self, state: Mapping[str, object]) -> None:
        if (
            self._watcher is None
            and self._watcher_retry_at is not None
            and self._watcher_retry_at > time.time()
        ):
            return
        providers = self._engine.catalog_providers(state)
        try:
            spec = await asyncio.to_thread(
                build_source_watch_spec,
                providers,
                self._engine.authorities,
            )
        except Exception:
            await self._degrade_for_watcher_failure()
            return
        if (
            self._watcher is not None
            and self._watcher.running
            and spec.fingerprint == self._watch_spec_fingerprint
        ):
            return
        await self._stop_watcher()
        watcher = self._watcher_factory()
        self._starting_watcher = watcher
        try:
            await watcher.start(spec, self._source_changed)
            await watcher.wait_ready()
        except asyncio.CancelledError:
            await watcher.shutdown()
            raise
        except Exception:
            logger.exception("model catalog source watcher failed to start")
            await watcher.shutdown()
            await self._degrade_for_watcher_failure()
            return
        finally:
            if self._starting_watcher is watcher:
                self._starting_watcher = None
        if self._closed:
            await watcher.shutdown()
            return
        self._watcher = watcher
        self._watch_spec_fingerprint = spec.fingerprint
        self._watcher_failure = False
        self._watcher_failures = 0
        self._watcher_retry_at = None
        self._watcher_guard = asyncio.create_task(
            self._guard_watcher(watcher),
            name="model-catalog-source-watcher-guard",
        )
        await self._close_watch_start_gap(providers)

    async def _close_watch_start_gap(self, providers: list[dict]) -> None:
        inspections = await self._engine.inspect_sources(providers)
        authorities = self._engine.authorities
        changed = False
        for provider, current in zip(providers, inspections, strict=True):
            previous = authorities.get(str(provider["id"]))
            if current.status == "available" and current.authority == previous:
                continue
            await self._engine.source_changed(provider, current)
            changed = True
        if changed:
            self._mark_dirty()

    async def _source_changed(self, _paths: tuple[str, ...]) -> None:
        self._mark_dirty()

    async def _guard_watcher(self, watcher: CatalogSourceWatcher) -> None:
        failure = await watcher.wait_stopped()
        if failure and self._watcher is watcher and not self._closed:
            self._watcher = None
            self._watch_spec_fingerprint = ""
            await watcher.shutdown()
            await self._degrade_for_watcher_failure()

    async def _degrade_for_watcher_failure(self) -> None:
        self._watcher_failure = True
        self._watcher_failures += 1
        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds
            * (2 ** min(self._watcher_failures - 1, 16)),
        )
        self._watcher_retry_at = time.time() + delay
        await self._engine.degrade_watcher()
        self._wake.set()

    def _next_timeout(self) -> float | None:
        deadlines = [
            timeout
            for timeout in (
                self._engine.next_timeout(),
                (
                    max(0.0, self._watcher_retry_at - time.time())
                    if self._watcher_retry_at is not None
                    else None
                ),
            )
            if timeout is not None
        ]
        return min(deadlines) if deadlines else None

    async def _stop_watcher(self) -> None:
        guard = self._watcher_guard
        self._watcher_guard = None
        if guard is not None:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        watcher = self._watcher
        self._watcher = None
        self._watch_spec_fingerprint = ""
        if watcher is not None:
            await watcher.shutdown()
        starting = self._starting_watcher
        self._starting_watcher = None
        if starting is not None and starting is not watcher:
            await starting.shutdown()


_fact_sinks: set[FactSink] = set()


async def _fanout_fact(fact: CatalogChangedFact) -> None:
    for sink in tuple(_fact_sinks):
        try:
            outcome = sink(fact)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.exception("model catalog fact subscriber failed")


catalog_refresh_owner = CatalogRefreshOwner(fact_sink=_fanout_fact)


def subscribe_fact_sink(sink: FactSink) -> Callable[[], None]:
    _fact_sinks.add(sink)
    return lambda: _fact_sinks.discard(sink)


async def start() -> None:
    await catalog_refresh_owner.start()


async def shutdown() -> None:
    await catalog_refresh_owner.shutdown()


def notify_provider_state_changed() -> None:
    catalog_refresh_owner.notify_provider_state_changed()


def notify_provider_auth_changed(provider_id: str) -> None:
    catalog_refresh_owner.notify_provider_auth_changed(provider_id)


async def request_refresh(provider_id: str) -> CatalogProjection:
    return await catalog_refresh_owner.refresh(provider_id)
