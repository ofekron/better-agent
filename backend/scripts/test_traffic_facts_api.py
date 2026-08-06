#!/usr/bin/env python3
"""Unit coverage for backend/traffic_facts_api.py (Phase G ingestion
boundary — see that module's docstring).

Same fresh-app-with-just-this-router recipe as
backend/scripts/test_adapter_api.py's `_build_app`, plus the bus-fact
collector pattern from backend/scripts/test_provider_runtime_facts.py.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_traffic_facts_api.py -q
    PYTHONPATH=. python3 backend/scripts/test_traffic_facts_api.py   # __main__ fallback
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-traffic-facts-api-")

import internal_guards  # noqa: E402
import traffic_facts_api  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_HEADERS = {"X-Internal-Token": "test-token"}
_VALID_BODY = {
    "facts": [
        {
            "type": traffic_facts_api.THREAD_STARTED,
            "thread_id": "thread-abc",
            "wire_session_id": "sess-xyz",
            "provider_id": "claude",
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "ts": "2026-08-06T00:00:00Z",
        },
    ],
}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(traffic_facts_api.router)
    return app


@pytest.fixture(autouse=True)
def _reset_authority(monkeypatch):
    """Every test controls its own authority state explicitly."""
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)
    yield


def test_flag_off_is_not_found(monkeypatch) -> None:
    monkeypatch.delenv(traffic_facts_api.ENV_FLAG, raising=False)
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: True)
    client = TestClient(_build_app())
    response = client.post("/api/internal/traffic-facts", json=_VALID_BODY, headers=_HEADERS)
    assert response.status_code == 404


def test_flag_on_missing_token_is_rejected_before_handler(monkeypatch) -> None:
    monkeypatch.setenv(traffic_facts_api.ENV_FLAG, "1")
    client = TestClient(_build_app())
    response = client.post("/api/internal/traffic-facts", json=_VALID_BODY)
    assert response.status_code == 422  # FastAPI's required-header validation, not our handler


def test_flag_on_unauthenticated_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv(traffic_facts_api.ENV_FLAG, "1")
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: False)
    client = TestClient(_build_app())
    response = client.post("/api/internal/traffic-facts", json=_VALID_BODY, headers=_HEADERS)
    assert response.status_code == 403


def test_flag_on_malformed_body_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv(traffic_facts_api.ENV_FLAG, "1")
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: True)
    client = TestClient(_build_app())
    for bad_body in (
        {},
        {"facts": []},
        {"facts": "not-a-list"},
        {"facts": [{"type": "traffic.not_a_real_type", "thread_id": "t1"}]},
        {"facts": [{"type": traffic_facts_api.THREAD_STARTED}]},  # missing thread_id
        {"facts": [{"type": traffic_facts_api.THREAD_STARTED, "thread_id": "t1", "usage": "nope"}]},
    ):
        response = client.post("/api/internal/traffic-facts", json=bad_body, headers=_HEADERS)
        assert response.status_code == 422, bad_body


def test_flag_on_valid_body_publishes_facts(monkeypatch) -> None:
    monkeypatch.setenv(traffic_facts_api.ENV_FLAG, "1")
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: True)

    async def scenario() -> None:
        received: list[BusEvent] = []
        delivered = asyncio.Event()

        async def capture(event: BusEvent) -> None:
            received.append(event)
            delivered.set()

        bus.subscribe(
            traffic_facts_api.THREAD_STARTED,
            capture,
            name="test_traffic_facts_api",
            bind_current_loop=True,
        )
        try:
            client = TestClient(_build_app())
            response = client.post("/api/internal/traffic-facts", json=_VALID_BODY, headers=_HEADERS)
            assert response.status_code == 200
            assert response.json() == {"accepted": 1}
            await asyncio.wait_for(delivered.wait(), timeout=1)
            assert len(received) == 1
            event = received[0]
            assert event.type == traffic_facts_api.THREAD_STARTED
            assert event.persist is False
            assert event.sid == "thread-abc"
            assert event.payload == {
                "thread_id": "thread-abc",
                "wire_session_id": "sess-xyz",
                "provider_id": "claude",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "ts": "2026-08-06T00:00:00Z",
            }
        finally:
            bus.unsubscribe("test_traffic_facts_api")

    asyncio.run(scenario())


def test_flag_on_multiple_facts_publish_in_order(monkeypatch) -> None:
    monkeypatch.setenv(traffic_facts_api.ENV_FLAG, "1")
    monkeypatch.setattr(internal_guards, "authority_is_valid", lambda: True)

    async def scenario() -> None:
        received: list[BusEvent] = []
        delivered = asyncio.Event()

        async def capture(event: BusEvent) -> None:
            received.append(event)
            if len(received) == 2:
                delivered.set()

        bus.subscribe(
            traffic_facts_api.THREAD_JOINED,
            capture,
            name="test_traffic_facts_api_multi",
            bind_current_loop=True,
        )
        try:
            body = {
                "facts": [
                    {
                        "type": traffic_facts_api.THREAD_JOINED,
                        "thread_id": "child-1",
                        "parent_thread_id": "parent-1",
                    },
                    {
                        "type": traffic_facts_api.THREAD_JOINED,
                        "thread_id": "child-2",
                        "parent_thread_id": "parent-1",
                    },
                ],
            }
            client = TestClient(_build_app())
            response = client.post("/api/internal/traffic-facts", json=body, headers=_HEADERS)
            assert response.status_code == 200
            assert response.json() == {"accepted": 2}
            await asyncio.wait_for(delivered.wait(), timeout=1)
            assert [event.sid for event in received] == ["child-1", "child-2"]
            assert [event.payload["parent_thread_id"] for event in received] == ["parent-1", "parent-1"]
        finally:
            bus.unsubscribe("test_traffic_facts_api_multi")

    asyncio.run(scenario())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
