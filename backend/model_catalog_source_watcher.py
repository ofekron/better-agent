from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from watchfiles import awatch


ChangeSink = Callable[[tuple[str, ...]], Awaitable[None] | None]
MAX_WATCH_ROOTS = 256


class CatalogSourceWatcherError(RuntimeError):
    pass


def _normalized_directory(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _normalized_path(path: Path) -> str:
    parent = Path(_normalized_directory(path.parent))
    return os.path.normcase(str(parent / path.name))


@dataclass(frozen=True)
class SourceWatchSpec:
    exact_paths: tuple[str, ...]
    search_directories: tuple[str, ...]
    search_names: tuple[str, ...]
    roots: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        exact_paths: Iterable[Path] = (),
        search_directories: Iterable[Path] = (),
        search_names: Iterable[str] = (),
    ) -> SourceWatchSpec:
        exact: set[str] = set()
        directories: set[str] = set()
        names = {
            name
            for name in search_names
            if type(name) is str and name and Path(name).name == name
        }
        for path in exact_paths:
            if not isinstance(path, Path) or not path.is_absolute():
                raise CatalogSourceWatcherError("watch path must be absolute")
            exact.add(_normalized_path(path))
            directories.add(_normalized_directory(path.parent))
        search: set[str] = set()
        for directory in search_directories:
            if not isinstance(directory, Path) or not directory.is_absolute():
                raise CatalogSourceWatcherError(
                    "watch directory must be absolute",
                )
            normalized = _normalized_directory(directory)
            search.add(normalized)
            directories.add(normalized)
        existing = tuple(
            sorted(
                directory
                for directory in directories
                if Path(directory).is_dir()
            ),
        )
        if not existing or len(existing) > MAX_WATCH_ROOTS:
            raise CatalogSourceWatcherError("invalid watch root set")
        return cls(
            exact_paths=tuple(sorted(exact)),
            search_directories=tuple(sorted(search)),
            search_names=tuple(sorted(names)),
            roots=existing,
        )

    @property
    def fingerprint(self) -> str:
        payload = "\0".join(
            (
                *self.exact_paths,
                "\1",
                *self.search_directories,
                "\2",
                *self.search_names,
            ),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def accepts(self, _change: object, raw_path: str) -> bool:
        path = Path(raw_path)
        if (
            _normalized_directory(path) in self.roots
            or _normalized_path(path) in self.exact_paths
        ):
            return True
        return (
            _normalized_directory(path.parent) in self.search_directories
            and path.name in self.search_names
        )


class CatalogSourceWatcher:
    def __init__(
        self,
        *,
        ready_timeout_ms: int = 250,
        debounce_ms: int = 100,
    ) -> None:
        if (
            type(ready_timeout_ms) is not int
            or ready_timeout_ms <= 0
            or type(debounce_ms) is not int
            or debounce_ms <= 0
        ):
            raise CatalogSourceWatcherError("invalid watcher timing")
        self._ready_timeout_ms = ready_timeout_ms
        self._debounce_ms = debounce_ms
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._failure = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def failure(self) -> str:
        return self._failure

    async def start(self, spec: SourceWatchSpec, sink: ChangeSink) -> None:
        if self.running or not isinstance(spec, SourceWatchSpec):
            raise CatalogSourceWatcherError("watcher already running")
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._failure = ""
        self._task = asyncio.create_task(
            self._run(spec, sink),
            name="model-catalog-source-watcher",
        )

    async def wait_ready(self) -> None:
        await self._ready.wait()
        if self._failure:
            raise CatalogSourceWatcherError(self._failure)

    async def wait_stopped(self) -> str:
        task = self._task
        if task is not None:
            await asyncio.shield(task)
        return self._failure

    async def shutdown(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await task
        finally:
            self._task = None

    async def _run(self, spec: SourceWatchSpec, sink: ChangeSink) -> None:
        try:
            async for changes in awatch(
                *spec.roots,
                watch_filter=spec.accepts,
                debounce=self._debounce_ms,
                step=max(1, self._debounce_ms // 4),
                stop_event=self._stop,
                rust_timeout=self._ready_timeout_ms,
                yield_on_timeout=True,
                force_polling=False,
                recursive=False,
            ):
                self._ready.set()
                changed = tuple(
                    sorted(
                        {
                            _normalized_path(Path(path))
                            for _change, path in changes
                        },
                    ),
                )
                if not changed:
                    continue
                outcome = sink(changed)
                if inspect.isawaitable(outcome):
                    await outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()
