#!/usr/bin/env python3
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

import asyncio
import atexit
import sys
import time
from pathlib import Path

import _test_home
_TEST_HOME = _test_home.TestHome.acquire("bc-test-extension-incident-delivery-")
atexit.register(_TEST_HOME.release)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import extension_backend_loader  # noqa: E402
import extension_incident_outbox  # noqa: E402
import extension_store  # noqa: E402
import node_client  # noqa: E402
import node_config_sync  # noqa: E402
import node_link  # noqa: E402
import node_store  # noqa: E402
from json_store import read_json  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


class _FakeClient:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail_send = fail_send

    async def send_extension_incident(self, incident: dict) -> None:
        if self.fail_send:
            raise RuntimeError("offline")
        self.sent.append(dict(incident))


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(dict(payload))


class _FakeConn:
    def __init__(self) -> None:
        self.ws = _FakeWs()


def _seed_extension(extension_id: str) -> str:
    activation_id = "a" * 32
    data = extension_store._load()
    data["extensions"][extension_id] = {
        "manifest": {
            "id": extension_id,
            "dependencies": [],
            "entrypoints": {"backend_timeouts": {"work": 2.0}},
        },
        "enabled": True,
        "activation_id": activation_id,
        "installed_at": "test",
        "updated_at": "test",
        "source": {"commit_sha": "test-generation"},
        "entitlement": {"status": "active"},
    }
    extension_store._save(data)
    return activation_id


def _history_for(extension_id: str) -> dict:
    history = read_json(extension_store._slow_calls_path(), {"extensions": {}})
    return history.get("extensions", {}).get(extension_id, {})


async def test_worker_incident_is_persisted_before_send_and_never_mutates_worker_store() -> None:
    fake = _FakeClient()
    original_singleton = node_client._singleton
    original_record = extension_store.record_slow_backend_call
    calls: list[str] = []

    def record_called(*_args, **_kwargs):
        calls.append("record")
        return []

    node_client._singleton = fake  # type: ignore[assignment]
    extension_store.record_slow_backend_call = record_called  # type: ignore[assignment]
    try:
        await extension_backend_loader._record_slow_call(  # type: ignore[attr-defined]
            {"extension_id": "ofek.remote-ext", "backend_timeouts": {"work": 0.01}},
            "b" * 32,
            "work",
            1.0,
        )
    finally:
        node_client._singleton = original_singleton  # type: ignore[assignment]
        extension_store.record_slow_backend_call = original_record  # type: ignore[assignment]

    pending = extension_incident_outbox.pending()
    check(len(pending) == 1, "worker persists exactly one pending incident")
    check(fake.sent == pending, "worker sends the persisted incident fact")
    check(calls == [], "worker incident path does not mutate extension_store")


async def test_reconnect_replays_until_ack_then_removes() -> None:
    extension_incident_outbox.enqueue(
        kind="backend_timeout",
        extension_id="ofek.replay-ext",
        activation_id="c" * 32,
        elapsed_seconds=9.0,
    )
    client = node_client.NodeClient()
    sent: list[dict] = []

    async def capture(incident: dict) -> None:
        sent.append(dict(incident))

    client.send_extension_incident = capture  # type: ignore[assignment]
    await client._replay_extension_incidents()  # type: ignore[attr-defined]
    check(len(sent) == 2, "reconnect replay ships all unacked incidents")
    first_id = sent[0]["incident_id"]
    await client._route_inbound({  # type: ignore[attr-defined]
        "type": "extension_incident_ack",
        "incident_id": first_id,
        "accepted": True,
    })
    remaining = extension_incident_outbox.pending()
    check(all(item["incident_id"] != first_id for item in remaining), "acked incident is absent from durable outbox")


async def test_worker_send_failure_keeps_persisted_incident_for_replay() -> None:
    fake = _FakeClient(fail_send=True)
    original_singleton = node_client._singleton
    original_record = extension_store.record_backend_timeout
    calls: list[str] = []

    def record_called(*_args, **_kwargs):
        calls.append("record")
        return []

    before = len(extension_incident_outbox.pending())
    node_client._singleton = fake  # type: ignore[assignment]
    extension_store.record_backend_timeout = record_called  # type: ignore[assignment]
    try:
        await extension_backend_loader._record_timeout(  # type: ignore[attr-defined]
            "ofek.offline-ext",
            "f" * 32,
            3.0,
        )
    finally:
        node_client._singleton = original_singleton  # type: ignore[assignment]
        extension_store.record_backend_timeout = original_record  # type: ignore[assignment]
    check(len(extension_incident_outbox.pending()) == before + 1, "send failure leaves incident persisted")
    check(calls == [], "send failure does not fall back to worker store mutation")


async def test_primary_dedups_ack_loss_replay_by_authenticated_node_id() -> None:
    extension_id = "ofek.primary-dedup"
    activation_id = _seed_extension(extension_id)
    conn = _FakeConn()
    original_conn = node_store.get_connection
    node_store.get_connection = lambda _node_id: conn  # type: ignore[assignment]
    try:
        msg = {
            "type": "extension_incident",
            "incident_id": "1" * 32,
            "node_id": "spoofed",
            "kind": "slow_backend_call",
            "extension_id": extension_id,
            "activation_id": activation_id,
            "elapsed_seconds": 3.0,
            "occurred_at": time.time(),
            "path": "work",
        }
        await node_link._handle_extension_incident("worker-a", msg)  # type: ignore[attr-defined]
        await node_link._handle_extension_incident("worker-a", msg)  # type: ignore[attr-defined]
    finally:
        node_store.get_connection = original_conn  # type: ignore[assignment]

    incidents = _history_for(extension_id).get("slow_host_elapsed", [])
    check(len(incidents) == 1, "primary dedups replayed incident id durably")
    check(incidents[0]["node_id"] == "worker-a", "primary uses authenticated node id for dedup scope")
    check(
        [ack["accepted"] for ack in conn.ws.sent] == [True, True],
        "primary acks replay only after handling",
    )


async def test_primary_rejects_stale_future_malformed_and_over_capacity() -> None:
    conn = _FakeConn()
    original_conn = node_store.get_connection
    node_store.get_connection = lambda _node_id: conn  # type: ignore[assignment]
    base = {
        "type": "extension_incident",
        "incident_id": "2" * 32,
        "kind": "backend_timeout",
        "extension_id": "ofek.reject-ext",
        "activation_id": "d" * 32,
        "elapsed_seconds": 9.0,
        "occurred_at": time.time(),
    }
    try:
        for patch in (
            {"occurred_at": time.time() - 3600},
            {"occurred_at": time.time() + 3600},
            {"elapsed_seconds": "bad"},
            {"path": "x" * 5000},
        ):
            msg = dict(base)
            msg.update(patch)
            await node_link._handle_extension_incident("worker-a", msg)  # type: ignore[attr-defined]
    finally:
        node_store.get_connection = original_conn  # type: ignore[assignment]

    reasons = [ack["reason"] for ack in conn.ws.sent]
    check(reasons == ["stale", "future_skew", "malformed", "capacity"], "primary rejects invalid incidents with terminal acks")
    check(_history_for("ofek.reject-ext") == {}, "rejected incidents do not mutate incident history")


async def test_primary_syncs_read_only_health_decision_state() -> None:
    extension_id = "ofek.primary-health-decision"
    activation_id = _seed_extension(extension_id)
    conn = _FakeConn()
    original_conn = node_store.get_connection
    original_broadcast = __import__("extension_api")._broadcast_extension_changed
    original_notify = node_config_sync.notify_changed
    broadcasts: list[str] = []
    syncs: list[str] = []

    async def broadcast(*_topics):
        broadcasts.append("broadcast")

    node_store.get_connection = lambda _node_id: conn  # type: ignore[assignment]
    __import__("extension_api")._broadcast_extension_changed = broadcast  # type: ignore[attr-defined]
    node_config_sync.notify_changed = lambda surface: syncs.append(surface)  # type: ignore[assignment]
    try:
        for idx in range(3):
            await node_link._handle_extension_incident("worker-a", {
                "type": "extension_incident",
                "incident_id": f"{idx + 3:032x}",
                "kind": "backend_timeout",
                "extension_id": extension_id,
                "activation_id": activation_id,
                "elapsed_seconds": 3.0,
                "occurred_at": time.time(),
            })  # type: ignore[attr-defined]
    finally:
        node_store.get_connection = original_conn  # type: ignore[assignment]
        __import__("extension_api")._broadcast_extension_changed = original_broadcast  # type: ignore[attr-defined]
        node_config_sync.notify_changed = original_notify  # type: ignore[assignment]

    record = extension_store.get_extension(extension_id) or {}
    decision = record.get("pending_health_decision") or {}
    check(
        record.get("enabled") is True
        and decision.get("attributed_extension_id") == extension_id,
        "primary owns pending extension health decision",
    )
    check(broadcasts == ["broadcast"], "primary broadcasts health decision once")
    check(syncs == ["extensions"], "primary syncs read-only extension projection to workers")


async def test_worker_outbox_capacity_does_not_fall_back_to_worker_store() -> None:
    fake = _FakeClient()
    original_singleton = node_client._singleton
    original_max = extension_incident_outbox.MAX_PENDING_INCIDENTS
    original_record = extension_store.record_backend_timeout
    calls: list[str] = []
    node_client._singleton = fake  # type: ignore[assignment]
    extension_incident_outbox.MAX_PENDING_INCIDENTS = len(extension_incident_outbox.pending())

    def record_called(*_args, **_kwargs):
        calls.append("record")
        return []

    extension_store.record_backend_timeout = record_called  # type: ignore[assignment]
    try:
        await extension_backend_loader._record_timeout(  # type: ignore[attr-defined]
            "ofek.capacity-ext",
            "e" * 32,
            3.0,
        )
    finally:
        node_client._singleton = original_singleton  # type: ignore[assignment]
        extension_incident_outbox.MAX_PENDING_INCIDENTS = original_max
        extension_store.record_backend_timeout = original_record  # type: ignore[assignment]
    check(calls == [], "full worker outbox still does not mutate worker store")
    check(fake.sent == [], "full worker outbox does not send an unpersisted incident")


async def test_worker_outbox_expires_stale_incidents_before_capacity_check() -> None:
    original_max = extension_incident_outbox.MAX_PENDING_INCIDENTS
    extension_incident_outbox.MAX_PENDING_INCIDENTS = len(extension_incident_outbox.pending()) + 1
    try:
        stale = extension_incident_outbox.enqueue(
            kind="backend_timeout",
            extension_id="ofek.expired-ext",
            activation_id="b" * 32,
            elapsed_seconds=3.0,
            occurred_at=time.time() - extension_incident_outbox.MAX_PENDING_INCIDENT_AGE_SECONDS - 1,
        )
        fresh = extension_incident_outbox.enqueue(
            kind="backend_timeout",
            extension_id="ofek.fresh-ext",
            activation_id="c" * 32,
            elapsed_seconds=3.0,
        )
    finally:
        extension_incident_outbox.MAX_PENDING_INCIDENTS = original_max

    pending_ids = {item["incident_id"] for item in extension_incident_outbox.pending()}
    check(stale["incident_id"] not in pending_ids, "worker outbox expires stale incidents")
    check(fresh["incident_id"] in pending_ids, "worker outbox frees capacity before enqueue")


async def _main() -> None:
    await test_worker_incident_is_persisted_before_send_and_never_mutates_worker_store()
    await test_reconnect_replays_until_ack_then_removes()
    await test_worker_send_failure_keeps_persisted_incident_for_replay()
    await test_primary_dedups_ack_loss_replay_by_authenticated_node_id()
    await test_primary_rejects_stale_future_malformed_and_over_capacity()
    await test_primary_syncs_read_only_health_decision_state()
    await test_worker_outbox_capacity_does_not_fall_back_to_worker_store()
    await test_worker_outbox_expires_stale_incidents_before_capacity_check()
    print("extension incident delivery tests passed")


if __name__ == "__main__":
    asyncio.run(_main())
