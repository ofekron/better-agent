from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import uuid
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-model-catalog-owner-")

import config_store  # noqa: E402
from model_catalog_refresh import (  # noqa: E402
    CatalogChangedFact,
    CatalogProjection,
    CatalogRefreshError,
    CatalogRefreshOwner,
)
import provider_sync_authority  # noqa: E402
from scripts.codex_model_discovery_test_support import (  # noqa: E402
    codex_on_path,
    provider,
    visible_catalog,
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


def _invocations(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return 0


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


def _replace_provider_generation(provider_id: str) -> str:
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


async def _scenario(root: Path) -> None:
    executable = root / "bin" / "codex"
    invocation_log = root / "invocations"
    record = provider(root)
    provider_id = record["id"]
    write_executable(
        executable,
        visible_catalog(),
        invocation_log=invocation_log,
    )
    facts: asyncio.Queue[CatalogChangedFact] = asyncio.Queue()

    with codex_on_path(executable):
        owner = CatalogRefreshOwner(fact_sink=facts.put_nowait)
        await owner.start()
        await owner.wait_ready()
        startup_invocations = _invocations(invocation_log)
        while not facts.empty():
            facts.get_nowait()

        first, second = await asyncio.gather(
            owner.refresh(provider_id),
            owner.refresh(provider_id),
        )
        assert first == second
        assert first == CatalogProjection(
            provider_id=provider_id,
            provider_generation=record["generation"],
            status="current",
            models=("alpha", "zeta"),
            models_current=True,
            retired=(),
            last_refreshed_at=first.last_refreshed_at,
            reason="",
            authority_fingerprint=first.authority_fingerprint,
        )
        manual_statuses = [
            fact.projection.status
            for fact in list(facts._queue)
            if fact.provider_id == provider_id
            and fact.projection is not None
        ]
        assert manual_statuses == ["refreshing", "current"], manual_statuses
        assert _invocations(invocation_log) == startup_invocations + 1
        after_manual = _invocations(invocation_log)
        await owner.shutdown()
        assert not owner.running

        restarted_facts: asyncio.Queue[CatalogChangedFact] = asyncio.Queue()
        restarted = CatalogRefreshOwner(fact_sink=restarted_facts.put_nowait)
        await restarted.start()
        await restarted.wait_ready()
        restored = restarted.snapshot(provider_id)
        assert restored is not None
        assert restored.status == "current"
        assert restored.models_current
        assert restored.models == ("alpha", "zeta")
        assert _invocations(invocation_log) == after_manual

        replacement = root / "bin" / "codex.next"
        write_executable(
            replacement,
            {"models": [{"slug": "alpha", "visibility": "list"}]},
            invocation_log=invocation_log,
        )
        os.replace(replacement, executable)
        retired_fact = await _next_fact(
            restarted_facts,
            lambda fact: (
                fact.provider_id == provider_id
                and fact.projection is not None
                and fact.projection.status == "current"
                and fact.projection.models == ("alpha",)
            ),
        )
        assert tuple(record.id for record in retired_fact.projection.retired) == (
            "zeta",
        )
        assert _invocations(invocation_log) > after_manual
        after_retirement = _invocations(invocation_log)

        replacement = root / "bin" / "codex.next"
        write_executable(
            replacement,
            {"models": []},
            exit_code=9,
            invocation_log=invocation_log,
        )
        os.replace(replacement, executable)
        failed = await _next_fact(
            restarted_facts,
            lambda fact: (
                fact.provider_id == provider_id
                and fact.projection is not None
                and fact.projection.status == "error"
            ),
        )
        assert failed.projection.models == ("alpha",)
        assert not failed.projection.models_current
        assert failed.projection.reason == "process_failed"
        assert _invocations(invocation_log) > after_retirement

        pid_file = root / "slow.pid"
        replacement = root / "bin" / "codex.next"
        write_executable(
            replacement,
            {"models": [{"slug": "new-generation", "visibility": "list"}]},
            delay_seconds=0.4,
            pid_file=pid_file,
            invocation_log=invocation_log,
        )
        os.replace(replacement, executable)
        refresh = asyncio.create_task(restarted.refresh(provider_id))
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await heartbeat.wait()
        await _wait_for_path(pid_file)

        replacement_generation = await asyncio.to_thread(
            _replace_provider_generation,
            provider_id,
        )
        restarted.notify_provider_state_changed()
        try:
            await refresh
        except CatalogRefreshError as exc:
            assert str(exc) == "provider changed"
        else:
            raise AssertionError("stale manual refresh was not cancelled")
        final = await restarted.refresh(provider_id)
        assert final.provider_generation == replacement_generation
        assert final.status == "current"
        assert final.models == ("new-generation",)

        idempotency_keys = [
            fact.idempotency_key
            for fact in list(restarted_facts._queue)
            if fact.provider_id == provider_id
        ]
        assert len(idempotency_keys) == len(set(idempotency_keys))
        await restarted.shutdown()
        assert not restarted.running


def test_catalog_refresh_owner_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as raw:
        asyncio.run(_scenario(Path(raw)))


if __name__ == "__main__":
    test_catalog_refresh_owner_lifecycle()
    print("PASS: model catalog refresh owner lifecycle")
