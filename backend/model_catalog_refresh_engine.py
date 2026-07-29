from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Awaitable, Callable, Mapping

import config_store
from codex_model_discovery import (
    DiscoveryResult,
    SourceInspection,
    discover_models,
    inspect_catalog_source,
    write_discovery_cache,
)
from model_catalog_authority import CatalogAuthority
from model_catalog_cache import CatalogSnapshot, read_catalog_cache
from model_catalog_paths import catalog_cache_path
from model_catalog_refresh_state import (
    CatalogChangedFact,
    CatalogProjection,
    changed_fact,
    removed_fact,
    retired_after_success,
)


logger = logging.getLogger(__name__)
FactSink = Callable[[CatalogChangedFact], Awaitable[None] | None]


def _read_provider_cache(
    provider_id: str,
    authority: CatalogAuthority,
):
    return read_catalog_cache(
        catalog_cache_path(provider_id),
        expected_authority=authority,
    )


def _write_provider_cache(
    provider_id: str,
    result: DiscoveryResult,
    retired: list[dict],
    refreshed_at: float,
) -> None:
    write_discovery_cache(
        catalog_cache_path(provider_id),
        result,
        retired=retired,
        refreshed_at=refreshed_at,
    )


class CatalogRefreshEngine:
    def __init__(
        self,
        *,
        wake: Callable[[], None],
        fact_sink: FactSink | None,
        max_age_seconds: float,
        retry_base_seconds: float,
        retry_max_seconds: float,
        max_concurrency: int,
    ) -> None:
        self._wake = wake
        self._fact_sink = fact_sink
        self._max_age_seconds = max_age_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._projections: dict[str, CatalogProjection] = {}
        self._snapshots: dict[str, CatalogSnapshot] = {}
        self._authorities: dict[str, CatalogAuthority] = {}
        self._next_due: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._live_generations: dict[str, str] = {}
        self._inflight: dict[
            tuple[str, str],
            asyncio.Task[CatalogProjection],
        ] = {}
        self._manual_inflight: dict[
            tuple[str, str],
            asyncio.Task[CatalogProjection],
        ] = {}
        self._reconcile_locks: dict[
            tuple[str, str],
            asyncio.Lock,
        ] = {}

    @property
    def authorities(self) -> dict[str, CatalogAuthority]:
        return dict(self._authorities)

    def snapshot(self, provider_id: str) -> CatalogProjection | None:
        return self._projections.get(provider_id)

    def bind_wake(self, wake: Callable[[], None]) -> None:
        self._wake = wake

    def next_timeout(self) -> float | None:
        if not self._next_due:
            return None
        return max(0.0, min(self._next_due.values()) - time.time())

    @staticmethod
    def catalog_providers(state: Mapping[str, object]) -> list[dict]:
        providers = state.get("providers")
        if type(providers) is not list:
            return []
        return [
            dict(provider)
            for provider in providers
            if type(provider) is dict
            and provider.get("kind") in {"codex", "fugu"}
            and provider.get("suspended") is not True
        ]

    async def shutdown(self) -> None:
        tasks = [
            *self._inflight.values(),
            *self._manual_inflight.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._manual_inflight.clear()
        self._reconcile_locks.clear()

    async def reconcile(self, state: Mapping[str, object]) -> None:
        providers = self.catalog_providers(state)
        live = {
            str(provider["id"]): str(provider["generation"])
            for provider in providers
        }
        previous_live = self._live_generations
        self._live_generations = live
        for provider_id, generation in previous_live.items():
            if live.get(provider_id) != generation:
                await self._cancel_provider_tasks(
                    provider_id,
                    keep_generation=live.get(provider_id),
                )
        retired_ids = {
            provider_id
            for provider_id, projection in self._projections.items()
            if live.get(provider_id) != projection.provider_generation
        }
        for provider_id in retired_ids:
            await self._remove_projection(provider_id)
        await asyncio.gather(
            *(
                self.reconcile_provider(provider, force=False)
                for provider in providers
            ),
        )

    async def _cancel_provider_tasks(
        self,
        provider_id: str,
        *,
        keep_generation: str | None,
    ) -> None:
        tasks = {
            task
            for key, task in (
                *self._inflight.items(),
                *self._manual_inflight.items(),
            )
            if key[0] == provider_id and key[1] != keep_generation
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight = {
            key: task
            for key, task in self._inflight.items()
            if task not in tasks
        }
        self._manual_inflight = {
            key: task
            for key, task in self._manual_inflight.items()
            if task not in tasks
        }

    async def _remove_projection(self, provider_id: str) -> None:
        previous = self._projections.pop(provider_id)
        self._snapshots.pop(provider_id, None)
        self._authorities.pop(provider_id, None)
        self._next_due.pop(provider_id, None)
        self._failures.pop(provider_id, None)
        self._reconcile_locks = {
            key: lock
            for key, lock in self._reconcile_locks.items()
            if key[0] != provider_id
        }
        await self._emit(
            removed_fact(provider_id, previous.provider_generation),
        )

    async def manual_refresh(self, provider_id: str) -> CatalogProjection:
        current = self._projections.get(provider_id)
        if current is None:
            raise RuntimeError("provider unavailable")
        key = (provider_id, current.provider_generation)
        task = self._manual_inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._manual_refresh(provider_id),
                name=f"model-catalog-manual-refresh:{provider_id}:{key[1]}",
            )
            self._manual_inflight[key] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if self._live_generations.get(provider_id) != key[1]:
                raise RuntimeError("provider changed") from exc
            raise
        finally:
            if task.done() and self._manual_inflight.get(key) is task:
                self._manual_inflight.pop(key, None)

    async def _manual_refresh(self, provider_id: str) -> CatalogProjection:
        state = await asyncio.to_thread(config_store.list_providers)
        provider = next(
            (
                record
                for record in self.catalog_providers(state)
                if record.get("id") == provider_id
            ),
            None,
        )
        if provider is None:
            raise RuntimeError("provider unavailable")
        if (
            self._live_generations.get(provider_id)
            != str(provider["generation"])
        ):
            raise RuntimeError("provider changed")
        await self.reconcile_provider(provider, force=True)
        projection = self._projections.get(provider_id)
        if projection is None:
            raise RuntimeError("catalog projection unavailable")
        return projection

    async def reconcile_provider(
        self,
        provider: dict,
        *,
        force: bool,
    ) -> None:
        key = (str(provider["id"]), str(provider["generation"]))
        lock = self._reconcile_locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self._semaphore:
                await self._reconcile_provider_locked(provider, force=force)

    async def _reconcile_provider_locked(
        self,
        provider: dict,
        *,
        force: bool,
    ) -> None:
        provider_id = str(provider["id"])
        generation = str(provider["generation"])
        previous = self._projections.get(provider_id)
        if previous is None or previous.provider_generation != generation:
            self._reconcile_locks = {
                key: lock
                for key, lock in self._reconcile_locks.items()
                if key[0] != provider_id or key[1] == generation
            }
            self._snapshots.pop(provider_id, None)
            self._authorities.pop(provider_id, None)
            self._next_due.pop(provider_id, None)
            self._failures.pop(provider_id, None)
            await self._project(
                self._projection(
                    provider,
                    status="pending",
                    reason="catalog_pending",
                ),
            )
        inspection = await inspect_catalog_source(provider_id)
        if not self._is_live(provider):
            return
        if inspection.status != "available":
            await self._inspection_failure(provider, inspection)
            return
        authority = inspection.authority
        assert authority is not None
        self._authorities[provider_id] = authority
        try:
            read = await asyncio.to_thread(
                _read_provider_cache,
                provider_id,
                authority,
            )
        except Exception:
            await self._publish_failure(
                provider,
                "error",
                "cache_unavailable",
                authority,
            )
            self._schedule_retry(provider_id)
            return
        if not self._is_live(provider):
            return
        if (
            read.snapshot is not None
            and read.snapshot.authority.provider_generation == generation
        ):
            self._snapshots[provider_id] = read.snapshot
        snapshot_is_fresh = False
        if read.status == "matching" and read.snapshot is not None:
            due_at = read.snapshot.last_refreshed_at + self._max_age_seconds
            snapshot_is_fresh = due_at > time.time()
            if not force and snapshot_is_fresh:
                self._failures.pop(provider_id, None)
                self._next_due[provider_id] = due_at
                await self._project(
                    self._projection(
                        provider,
                        status="current",
                        reason="",
                        authority=authority,
                        models_current=True,
                    ),
                )
                return
        if not snapshot_is_fresh:
            await self._project(
                self._projection(
                    provider,
                    status=(
                        "stale"
                        if provider_id in self._snapshots
                        else "pending"
                    ),
                    reason=read.status,
                    authority=authority,
                ),
            )
        await self._refresh_singleflight(provider, authority)

    async def _inspection_failure(
        self,
        provider: dict,
        inspection: SourceInspection,
    ) -> None:
        status = (
            inspection.status
            if inspection.status in {"unsupported", "unavailable"}
            else "error"
        )
        await self._project(
            self._projection(
                provider,
                status=status,
                reason=inspection.reason,
            ),
        )
        if status == "unsupported":
            self._next_due.pop(str(provider["id"]), None)
        else:
            self._schedule_retry(str(provider["id"]))

    async def _refresh_singleflight(
        self,
        provider: dict,
        authority: CatalogAuthority,
    ) -> CatalogProjection:
        key = (str(provider["id"]), str(provider["generation"]))
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._execute_refresh(provider, authority),
                name=f"model-catalog-refresh:{key[0]}:{key[1]}",
            )
            self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _execute_refresh(
        self,
        provider: dict,
        authority: CatalogAuthority,
    ) -> CatalogProjection:
        provider_id = str(provider["id"])
        cached = self._snapshots.get(provider_id)
        await self._project(
            self._projection(
                provider,
                status="refreshing",
                reason="",
                authority=authority,
                models_current=(
                    cached is not None and cached.authority == authority
                ),
            ),
        )
        result = await discover_models(provider_id)
        if not self._is_live(provider):
            return self._projection(
                provider,
                status="stale",
                reason="provider_changed",
                authority=authority,
            )
        if result.status != "ok":
            return await self._discovery_failure(provider, result, authority)
        refreshed_at = time.time()
        retired = retired_after_success(
            cached,
            result.models,
            absent_at=refreshed_at,
        )
        try:
            await asyncio.to_thread(
                _write_provider_cache,
                provider_id,
                result,
                [record.to_dict() for record in retired],
                refreshed_at,
            )
        except Exception:
            self._schedule_retry(provider_id)
            return await self._publish_failure(
                provider,
                "error",
                "cache_commit_unavailable",
                authority,
            )
        if not self._is_live(provider):
            return self._projection(
                provider,
                status="stale",
                reason="provider_changed",
                authority=authority,
            )
        current = await inspect_catalog_source(provider_id)
        if not self._is_live(provider):
            return self._projection(
                provider,
                status="stale",
                reason="provider_changed",
                authority=authority,
            )
        if current.status != "available" or current.authority != result.authority:
            self._schedule_retry(provider_id)
            return await self._publish_failure(
                provider,
                "stale",
                "authority_changed",
                current.authority,
            )
        try:
            read = await asyncio.to_thread(
                _read_provider_cache,
                provider_id,
                current.authority,
            )
        except Exception:
            self._schedule_retry(provider_id)
            return await self._publish_failure(
                provider,
                "error",
                "cache_unavailable",
                current.authority,
            )
        if not self._is_live(provider):
            return self._projection(
                provider,
                status="stale",
                reason="provider_changed",
                authority=current.authority,
            )
        if read.status != "matching" or read.snapshot is None:
            self._schedule_retry(provider_id)
            return await self._publish_failure(
                provider,
                "error",
                "cache_commit_unavailable",
                current.authority,
            )
        self._authorities[provider_id] = current.authority
        self._snapshots[provider_id] = read.snapshot
        self._failures.pop(provider_id, None)
        self._next_due[provider_id] = (
            read.snapshot.last_refreshed_at + self._max_age_seconds
        )
        projection = self._projection(
            provider,
            status="current",
            reason="",
            authority=current.authority,
            models_current=True,
        )
        await self._project(projection)
        return projection

    async def _publish_failure(
        self,
        provider: Mapping[str, object],
        status: str,
        reason: str,
        authority: CatalogAuthority | None,
    ) -> CatalogProjection:
        projection = self._projection(
            provider,
            status=status,
            reason=reason,
            authority=authority,
        )
        await self._project(projection)
        return projection

    async def _discovery_failure(
        self,
        provider: dict,
        result: DiscoveryResult,
        authority: CatalogAuthority,
    ) -> CatalogProjection:
        status = (
            result.status
            if result.status in {"unsupported", "unavailable"}
            else "error"
        )
        projection = await self._publish_failure(
            provider,
            status,
            result.reason,
            authority,
        )
        if status == "unsupported":
            self._next_due.pop(str(provider["id"]), None)
        else:
            self._schedule_retry(str(provider["id"]))
        return projection

    async def source_changed(
        self,
        provider: dict,
        inspection: SourceInspection,
    ) -> None:
        if not self._is_live(provider):
            return
        if inspection.status != "available":
            await self._inspection_failure(provider, inspection)
            return
        self._authorities[str(provider["id"])] = inspection.authority
        await self._project(
            self._projection(
                provider,
                status="stale",
                reason="source_changed",
                authority=inspection.authority,
            ),
        )

    async def inspect_sources(
        self,
        providers: list[dict],
    ) -> list[SourceInspection]:
        async def inspect_one(provider: dict) -> SourceInspection:
            async with self._semaphore:
                return await inspect_catalog_source(str(provider["id"]))

        return await asyncio.gather(
            *(inspect_one(provider) for provider in providers),
        )

    async def degrade_watcher(self) -> None:
        for projection in list(self._projections.values()):
            await self._project(
                CatalogProjection(
                    provider_id=projection.provider_id,
                    provider_generation=projection.provider_generation,
                    status="error",
                    models=projection.models,
                    models_current=False,
                    retired=projection.retired,
                    last_refreshed_at=projection.last_refreshed_at,
                    reason="watcher_unavailable",
                    authority_fingerprint=projection.authority_fingerprint,
                ),
            )

    def _schedule_retry(self, provider_id: str) -> None:
        attempts = self._failures.get(provider_id, 0) + 1
        self._failures[provider_id] = attempts
        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** min(attempts - 1, 16)),
        )
        self._next_due[provider_id] = time.time() + delay
        self._wake()

    def _is_live(self, provider: Mapping[str, object]) -> bool:
        return self._live_generations.get(str(provider["id"])) == str(
            provider["generation"],
        )

    def _projection(
        self,
        provider: Mapping[str, object],
        *,
        status: str,
        reason: str,
        authority: CatalogAuthority | None = None,
        models_current: bool = False,
    ) -> CatalogProjection:
        provider_id = str(provider["id"])
        snapshot = self._snapshots.get(provider_id)
        return CatalogProjection(
            provider_id=provider_id,
            provider_generation=str(provider["generation"]),
            status=status,
            models=snapshot.models if snapshot is not None else (),
            models_current=models_current and snapshot is not None,
            retired=snapshot.retired if snapshot is not None else (),
            last_refreshed_at=(
                snapshot.last_refreshed_at if snapshot is not None else 0.0
            ),
            reason=reason,
            authority_fingerprint=(
                authority.fingerprint if authority is not None else ""
            ),
        )

    async def _project(self, projection: CatalogProjection) -> None:
        if (
            self._live_generations.get(projection.provider_id)
            != projection.provider_generation
        ):
            return
        if self._projections.get(projection.provider_id) == projection:
            return
        self._projections[projection.provider_id] = projection
        await self._emit(
            changed_fact(
                projection,
                self._snapshots.get(projection.provider_id),
            ),
        )

    async def _emit(self, fact: CatalogChangedFact) -> None:
        if self._fact_sink is None:
            return
        try:
            outcome = self._fact_sink(fact)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.exception("model catalog fact sink failed")
