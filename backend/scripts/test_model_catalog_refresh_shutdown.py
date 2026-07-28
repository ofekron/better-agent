from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-catalog-shutdown-")

from model_catalog_refresh import CatalogRefreshOwner  # noqa: E402
from model_catalog_refresh_state import CatalogChangedFact  # noqa: E402
from model_catalog_source_watcher import MAX_WATCH_ROOTS  # noqa: E402
from scripts.codex_model_discovery_test_support import (  # noqa: E402
    codex_on_path,
    provider,
    write_executable,
)
from watchfiles import awatch  # noqa: E402


class _BlockingWatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.shutdown_called = False

    @property
    def running(self) -> bool:
        return self.started.is_set() and not self.released.is_set()

    async def start(self, _spec, _sink) -> None:
        self.started.set()

    async def wait_ready(self) -> None:
        await self.released.wait()

    async def wait_stopped(self) -> str:
        await self.released.wait()
        return ""

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.released.set()


async def _wait_for_path(path: Path) -> None:
    if path.exists():
        return
    async with asyncio.timeout(3):
        async for _changes in awatch(
            path.parent,
            force_polling=False,
            recursive=False,
        ):
            if path.exists():
                return


async def _shutdown_reaps_discovery(root: Path) -> None:
    executable = root / "bin" / "codex"
    pid_file = root / "catalog.pid"
    provider(root)
    write_executable(
        executable,
        {"models": [{"slug": "slow", "visibility": "list"}]},
        delay_seconds=5,
        pid_file=pid_file,
    )
    with codex_on_path(executable):
        owner = CatalogRefreshOwner()
        await owner.start()
        await _wait_for_path(pid_file)
        pid = int(pid_file.read_text(encoding="utf-8"))
        await owner.shutdown()

    assert not owner.running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("catalog discovery child survived owner shutdown")


async def _watcher_failure_is_truthful(root: Path) -> None:
    directories = [root / f"path-{index}" for index in range(MAX_WATCH_ROOTS + 1)]
    for directory in directories:
        directory.mkdir()
    executable = directories[0] / "codex"
    record = provider(root / "provider")
    write_executable(
        executable,
        {"models": [{"slug": "known", "visibility": "list"}]},
    )
    previous_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join(str(path) for path in directories)
    owner = CatalogRefreshOwner(
        retry_base_seconds=0.1,
        retry_max_seconds=0.1,
    )
    try:
        await owner.start()
        await owner.wait_ready()
        projection = owner.snapshot(record["id"])
        assert projection is not None
        assert projection.status == "error"
        assert projection.reason == "watcher_unavailable"
        assert projection.models == ("known",)
        assert not projection.models_current

        manual = await owner.refresh(record["id"])
        assert manual.status == "error"
        assert manual.reason == "watcher_unavailable"
        assert manual.models == ("known",)
        assert not manual.models_current

        os.environ["PATH"] = str(directories[0])
        async with asyncio.timeout(3):
            while True:
                recovered = owner.snapshot(record["id"])
                if recovered is not None and recovered.status == "current":
                    break
                await asyncio.sleep(0)
        assert recovered.models == ("known",)
        assert recovered.models_current
    finally:
        await owner.shutdown()
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path


async def _shutdown_reaps_starting_watcher() -> None:
    watcher = _BlockingWatcher()
    owner = CatalogRefreshOwner(watcher_factory=lambda: watcher)
    await owner.start()
    await watcher.started.wait()
    await owner.shutdown()
    assert watcher.shutdown_called
    assert not watcher.running
    assert not owner.running


async def _invalid_cache_namespace_is_provider_scoped(root: Path) -> None:
    cache_namespace = Path(TEST_HOME.path) / "model_catalog"
    if cache_namespace.is_symlink():
        cache_namespace.unlink()
    elif cache_namespace.exists():
        shutil.rmtree(cache_namespace)
    cache_namespace.symlink_to(root)
    executable = root / "bin" / "codex"
    record = provider(root)
    write_executable(
        executable,
        {"models": [{"slug": "known", "visibility": "list"}]},
    )
    facts: asyncio.Queue[CatalogChangedFact] = asyncio.Queue()
    with codex_on_path(executable):
        owner = CatalogRefreshOwner(fact_sink=facts.put_nowait)
        await owner.start()
        await owner.wait_ready()
        projection = owner.snapshot(record["id"])
        assert projection is not None
        assert projection.status == "error"
        assert projection.reason == "cache_unavailable"
        assert owner.running
        await owner.shutdown()
    cache_namespace.unlink()


def test_shutdown_reaps_active_discovery() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_shutdown_reaps_discovery(Path(raw)))


def test_watcher_failure_never_claims_current_catalog() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_watcher_failure_is_truthful(Path(raw)))


def test_shutdown_reaps_watcher_during_startup() -> None:
    asyncio.run(_shutdown_reaps_starting_watcher())


def test_invalid_cache_namespace_does_not_kill_owner() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_invalid_cache_namespace_is_provider_scoped(Path(raw)))


if __name__ == "__main__":
    test_shutdown_reaps_active_discovery()
    test_watcher_failure_never_claims_current_catalog()
    test_shutdown_reaps_watcher_during_startup()
    test_invalid_cache_namespace_does_not_kill_owner()
    print("PASS: catalog owner shutdown and watcher failure")
