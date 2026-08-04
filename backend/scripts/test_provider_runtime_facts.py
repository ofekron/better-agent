from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-provider-runtime-facts-")

import config_store  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
import model_catalog_refresh  # noqa: E402
import node_config_sync  # noqa: E402
import provider_auth  # noqa: E402
import provider_runtime_facts  # noqa: E402


def test_fact_fanout_coalesces_without_runtime_details() -> None:
    async def scenario() -> None:
        received: list[BusEvent] = []
        delivered = asyncio.Event()

        async def capture(event: BusEvent) -> None:
            received.append(event)
            delivered.set()

        bus.subscribe(
            provider_runtime_facts.EVENT_TYPE,
            capture,
            name="test_provider_runtime_facts",
            bind_current_loop=True,
        )
        try:
            def notify_from_worker() -> None:
                provider_runtime_facts.notify_config_committed()
                provider_runtime_facts.notify_config_committed()
                provider_runtime_facts.notify_auth_committed()

            producer = threading.Thread(target=notify_from_worker)
            producer.start()
            producer.join(timeout=1)
            assert not producer.is_alive()
            provider_runtime_facts.bind_loop()
            await asyncio.wait_for(delivered.wait(), timeout=1)
            await asyncio.sleep(0)

            assert len(received) == 1
            assert received[0].payload == {"causes": ["auth", "config"]}
            assert received[0].persist is False
            assert received[0].root_id == ""
            assert received[0].sid == ""
        finally:
            bus.unsubscribe("test_provider_runtime_facts")
            await provider_runtime_facts.shutdown()

    asyncio.run(scenario())


def test_closed_loop_rebind_flushes_queued_fact() -> None:
    loop_a = asyncio.new_event_loop()

    def queue_without_flushing() -> None:
        provider_runtime_facts.bind_loop()
        provider_runtime_facts.notify_config_committed()
        loop_a.stop()

    loop_a.call_soon(queue_without_flushing)
    loop_a.run_forever()
    loop_a.close()

    async def scenario() -> None:
        delivered = asyncio.Event()

        async def capture(_event: BusEvent) -> None:
            delivered.set()

        bus.subscribe(
            provider_runtime_facts.EVENT_TYPE,
            capture,
            name="test_provider_runtime_facts_rebind",
            bind_current_loop=True,
        )
        try:
            provider_runtime_facts.bind_loop()
            await asyncio.wait_for(delivered.wait(), timeout=1)
        finally:
            bus.unsubscribe("test_provider_runtime_facts_rebind")
            await provider_runtime_facts.shutdown()

    asyncio.run(scenario())


def test_only_durable_config_commit_notifies_runtime_fact() -> None:
    calls: list[str] = []
    original_fact = provider_runtime_facts.notify_config_committed
    original_catalog = model_catalog_refresh.notify_provider_state_changed
    original_node_sync = node_config_sync.notify_changed
    original_auth = provider_auth.notify_provider_config_changed
    provider_runtime_facts.notify_config_committed = lambda: calls.append("runtime")
    model_catalog_refresh.notify_provider_state_changed = lambda: None
    node_config_sync.notify_changed = lambda _scope: None
    provider_auth.notify_provider_config_changed = lambda: None
    try:
        created = config_store.add_provider({
            "name": "Runtime fact test",
            "kind": "claude",
            "mode": "subscription",
        })
        assert created["id"]
        assert config_store.delete_provider("missing") == (False, "missing")
    finally:
        provider_runtime_facts.notify_config_committed = original_fact
        model_catalog_refresh.notify_provider_state_changed = original_catalog
        node_config_sync.notify_changed = original_node_sync
        provider_auth.notify_provider_config_changed = original_auth
    assert calls == ["runtime"]


def test_auth_fact_only_follows_fingerprint_guarded_commit() -> None:
    async def scenario() -> None:
        facts: list[str] = []
        reconciles: list[str] = []
        original_catalog = provider_auth._notify_catalog_auth_changed
        original_config_fact = provider_runtime_facts.notify_config_committed
        original_fact = provider_runtime_facts.notify_auth_committed
        original_reconcile = provider_auth._request_config_reconcile
        provider_auth._notify_catalog_auth_changed = lambda _provider_id: None
        provider_runtime_facts.notify_config_committed = lambda: None
        provider_runtime_facts.notify_auth_committed = lambda: facts.append("auth")
        provider_auth._request_config_reconcile = lambda: reconciles.append("reconcile")
        try:
            provider = config_store.add_provider({
                "name": "Auth fact test",
                "kind": "claude",
                "mode": "subscription",
            })
            fingerprint = provider_auth._auth_fingerprint(provider)
            assert fingerprint is not None
            stale = (
                fingerprint[0],
                fingerprint[1],
                fingerprint[2] + 1,
                fingerprint[3],
                fingerprint[4],
                fingerprint[5],
            )
            committed = await provider_auth._commit_auth_status(
                provider["id"],
                stale,
                False,
            )
            assert not committed
            assert facts == []
            assert reconciles == ["reconcile"]

            committed = await provider_auth._commit_auth_status(
                provider["id"],
                fingerprint,
                True,
            )
            assert committed
            assert facts == ["auth"]
        finally:
            provider_auth._notify_catalog_auth_changed = original_catalog
            provider_runtime_facts.notify_config_committed = original_config_fact
            provider_runtime_facts.notify_auth_committed = original_fact
            provider_auth._request_config_reconcile = original_reconcile
            if "provider" in locals():
                provider_auth._auth_cache.pop(provider["id"], None)
                provider_auth._provider_auth_fingerprints.pop(provider["id"], None)

    asyncio.run(scenario())


if __name__ == "__main__":
    test_fact_fanout_coalesces_without_runtime_details()
    test_closed_loop_rebind_flushes_queued_fact()
    test_only_durable_config_commit_notifies_runtime_fact()
    test_auth_fact_only_follows_fingerprint_guarded_commit()
    print("OK: provider runtime facts")
