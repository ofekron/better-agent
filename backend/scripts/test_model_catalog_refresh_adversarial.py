from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-catalog-adversarial-")

import config_store  # noqa: E402
from model_catalog_refresh import CatalogChangedFact, CatalogRefreshOwner  # noqa: E402
import provider_sync_authority  # noqa: E402
from scripts.codex_model_discovery_test_support import (  # noqa: E402
    codex_on_path,
    provider,
    write_executable,
)
from watchfiles import awatch  # noqa: E402


async def _next_fact(
    queue: asyncio.Queue[CatalogChangedFact],
    predicate,
) -> CatalogChangedFact:
    async with asyncio.timeout(5):
        while True:
            fact = await queue.get()
            if predicate(fact):
                return fact


async def _wait_for_invocations(path: Path, count: int) -> None:
    if path.exists() and len(path.read_text(encoding="utf-8").splitlines()) >= count:
        return
    async with asyncio.timeout(5):
        async for _changes in awatch(
            path.parent,
            force_polling=False,
            recursive=False,
        ):
            if (
                path.exists()
                and len(path.read_text(encoding="utf-8").splitlines()) >= count
            ):
                return


def _replace_generation(provider_id: str) -> str:
    payload = config_store.export_provider_sync_state()
    replacement = copy.deepcopy(
        next(record for record in payload["providers"] if record["id"] == provider_id),
    )
    replacement["generation"] = str(uuid.uuid4())
    replacement["revision"] = 0
    providers = [
        replacement if record["id"] == provider_id else record
        for record in payload["providers"]
    ]
    payload["providers"] = providers
    payload["provider_state_authority"] = provider_sync_authority.advance_authority(
        payload["provider_state_authority"],
        payload["default_provider_id"],
        providers,
    )
    config_store.import_provider_sync_state(payload)
    return replacement["generation"]


def _suspend_all_catalog_providers() -> None:
    for record in config_store.list_providers()["providers"]:
        if (
            record.get("kind") in {"codex", "fugu"}
            and record.get("suspended") is not True
        ):
            config_store.set_provider_suspended(str(record["id"]), True)


async def _generation_fence(root: Path) -> None:
    _suspend_all_catalog_providers()
    executable = root / "bin" / "codex"
    pid_file = root / "slow.pid"
    record = provider(root)
    write_executable(
        executable,
        {"models": [{"slug": "old", "visibility": "list"}]},
        delay_seconds=1,
        pid_file=pid_file,
    )
    facts: asyncio.Queue[CatalogChangedFact] = asyncio.Queue()
    with codex_on_path(executable):
        owner = CatalogRefreshOwner(fact_sink=facts.put_nowait)
        await owner.start()
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0)
        while not facts.empty():
            facts.get_nowait()
        replacement_generation = await asyncio.to_thread(
            _replace_generation,
            record["id"],
        )
        owner.notify_provider_state_changed()
        observed: list[CatalogChangedFact] = []
        async with asyncio.timeout(5):
            while True:
                fact = await facts.get()
                observed.append(fact)
                if (
                    fact.projection is not None
                    and fact.projection.provider_generation
                    == replacement_generation
                ):
                    break
        removed_at = next(
            index
            for index, fact in enumerate(observed)
            if fact.kind == "catalog_removed"
        )
        for fact in observed[removed_at + 1 :]:
            if fact.projection is not None:
                assert fact.projection.provider_generation != record["generation"]
        await owner.wait_ready()
        await owner.shutdown()

    for fact in list(facts._queue):
        if fact.projection is not None:
            assert fact.projection.provider_generation != record["generation"]


async def _auth_and_suspension_invalidation(root: Path) -> None:
    _suspend_all_catalog_providers()
    config = root / "config"
    config.mkdir()
    (config / "config.toml").write_text("", encoding="utf-8")
    executable = root / "bin" / "codex"
    invocation_log = root / "invocations"
    record = provider(root, config_dir=config)
    write_executable(
        executable,
        {"models": [{"slug": "known", "visibility": "list"}]},
        invocation_log=invocation_log,
    )
    facts: asyncio.Queue[CatalogChangedFact] = asyncio.Queue()
    with codex_on_path(executable):
        owner = CatalogRefreshOwner(fact_sink=facts.put_nowait)
        await owner.start()
        await owner.wait_ready()
        initial = owner.snapshot(record["id"])
        assert initial is not None
        initial_authority = initial.authority_fingerprint
        initial_invocations = len(
            invocation_log.read_text(encoding="utf-8").splitlines(),
        )

        replacement = config / "auth.next"
        replacement.write_text('{"tokens":{"access_token":"secret"}}', encoding="utf-8")
        os.replace(replacement, config / "auth.json")
        refreshed = await _next_fact(
            facts,
            lambda fact: (
                fact.provider_id == record["id"]
                and fact.projection is not None
                and fact.projection.status == "current"
                and fact.projection.authority_fingerprint != initial_authority
            ),
        )
        assert refreshed.projection is not None
        assert len(invocation_log.read_text(encoding="utf-8").splitlines()) > (
            initial_invocations
        )

        config_store.set_provider_suspended(record["id"], True)
        owner.notify_provider_state_changed()
        async with asyncio.timeout(5):
            while owner.snapshot(record["id"]) is not None:
                await asyncio.sleep(0)
        removed = next(
            fact
            for fact in list(facts._queue)
            if fact.kind == "catalog_removed"
            and fact.provider_id == record["id"]
        )
        assert removed.projection is None
        assert owner.snapshot(record["id"]) is None
        await owner.shutdown()


async def _missing_path_becomes_preferred(root: Path) -> None:
    _suspend_all_catalog_providers()
    missing = root / "preferred" / "bin"
    fallback = root / "fallback"
    fallback_executable = fallback / "codex"
    record = provider(root)
    write_executable(
        fallback_executable,
        {"models": [{"slug": "fallback", "visibility": "list"}]},
    )
    previous_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join((str(missing), str(fallback)))
    facts: asyncio.Queue[CatalogChangedFact] = asyncio.Queue()
    owner = CatalogRefreshOwner(fact_sink=facts.put_nowait)
    try:
        await owner.start()
        await owner.wait_ready()
        assert owner.snapshot(record["id"]).models == ("fallback",)

        staging = root / "staging"
        write_executable(
            staging / "codex",
            {"models": [{"slug": "preferred", "visibility": "list"}]},
        )
        missing.parent.mkdir()
        os.replace(staging, missing)
        changed = await _next_fact(
            facts,
            lambda fact: (
                fact.provider_id == record["id"]
                and fact.projection is not None
                and fact.projection.status == "current"
                and fact.projection.models == ("preferred",)
            ),
        )
        assert changed.projection is not None
    finally:
        await owner.shutdown()
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path


async def _retry_is_live_and_bounded(root: Path) -> None:
    _suspend_all_catalog_providers()
    executable = root / "bin" / "codex"
    invocation_log = root / "invocations"
    provider(root)
    write_executable(
        executable,
        {"models": []},
        delay_seconds=0.05,
        exit_code=9,
        invocation_log=invocation_log,
    )
    with codex_on_path(executable):
        owner = CatalogRefreshOwner(
            retry_base_seconds=0.2,
            retry_max_seconds=0.2,
        )
        await owner.start()
        await owner.wait_ready()
        await _wait_for_invocations(invocation_log, 2)
        await asyncio.sleep(0.12)
        assert len(invocation_log.read_text(encoding="utf-8").splitlines()) == 2
        await owner.shutdown()


async def _global_concurrency_bound(root: Path) -> None:
    _suspend_all_catalog_providers()
    executable = root / "bin" / "codex"
    write_executable(
        executable,
        {"models": [{"slug": "known", "visibility": "list"}]},
        delay_seconds=0.35,
    )
    for index in range(4):
        provider(root / f"provider-{index}")
    with codex_on_path(executable):
        owner = CatalogRefreshOwner(max_concurrency=2)
        started = time.monotonic()
        await owner.start()
        await owner.wait_ready()
        elapsed = time.monotonic() - started
        await owner.shutdown()
    assert elapsed >= 0.6


def test_auth_suspension_and_missing_path_invalidate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_auth_and_suspension_invalidation(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_missing_path_becomes_preferred(Path(raw)))


def test_retry_liveness_and_global_concurrency_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_retry_is_live_and_bounded(Path(raw)))
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_global_concurrency_bound(Path(raw)))


def test_generation_fences_stale_inflight_work() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_generation_fence(Path(raw)))


if __name__ == "__main__":
    test_auth_suspension_and_missing_path_invalidate()
    test_retry_liveness_and_global_concurrency_bound()
    test_generation_fences_stale_inflight_work()
    print("PASS: adversarial catalog refresh lifecycle")
