#!/usr/bin/env python3
"""100% unit coverage for startup_tasks.

Covers the adapter contract: every startup step is registered as a
`background_work` item owned by core/startup with a longer success
retention, then finished on success or marked failed on exception. The
three `run_task` execution branches (coroutine fn, sync fn offloaded via
`asyncio.to_thread`, sync fn awaited inline) and both exception handlers
are driven deterministically with a fake registry, plus the composite
multi-await variant. Test-home guard matches repo convention.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-startup-tasks-")
import atexit  # noqa: E402

atexit.register(TEST_HOME.release)

import background_work  # noqa: E402
import startup_tasks  # noqa: E402
from startup_tasks import run_composite_task, run_task  # noqa: E402


class _FakeRegistry:
    """Records report/finish calls; returns ascending item ids."""

    def __init__(self) -> None:
        self.reports: list[dict] = []
        self.finishes: list[dict] = []
        self._n = 0

    def report(self, **kwargs):
        self.reports.append(kwargs)
        self._n += 1
        return f"item-{self._n}"

    def finish(self, item_id, *, status="succeeded", error=None):
        self.finishes.append(
            {"item_id": item_id, "status": status, "error": error}
        )
        return True


@pytest.fixture
def registry(monkeypatch):
    fake = _FakeRegistry()
    monkeypatch.setattr(startup_tasks, "background_work_registry", fake)
    return fake


def test_owner_and_retention_constants():
    assert startup_tasks._OWNER == "startup"
    assert startup_tasks._RETENTION_MS == 2500


def test_begin_registers_core_startup_owner(registry):
    item_id = startup_tasks._begin("scan", "startup.task.scan")

    assert item_id == "item-1"
    assert registry.reports == [
        {
            "owner_kind": background_work.OWNER_CORE,
            "owner_id": "startup",
            "local_id": "scan",
            "label": "startup.task.scan",
            "title_key": "startup.task.scan",
            "retention_ms": 2500,
        }
    ]


def test_run_task_async_success_finishes(registry):
    async def fn(x, *, y):
        return x + y

    asyncio.run(run_task("migrate", "startup.task.migrate", fn, 1, y=2))

    assert registry.reports[0]["local_id"] == "migrate"
    assert registry.finishes == [{"item_id": "item-1", "status": "succeeded", "error": None}]


def test_run_task_async_failure_marks_failed(registry, caplog):
    async def fn():
        raise ValueError("boom")

    caplog.set_level(logging.ERROR, logger="startup_tasks")
    # A failed startup task must not propagate — run_task swallows it.
    asyncio.run(run_task("replay", "startup.task.replay", fn))

    assert registry.finishes == [
        {
            "item_id": "item-1",
            "status": background_work.STATUS_FAILED,
            "error": "boom",
        }
    ]
    assert any("startup task replay failed" in r.message for r in caplog.records)


def test_run_task_sync_in_thread_success_finishes(registry):
    def fn(a, b):
        return a * b

    asyncio.run(run_task("warm", "startup.task.warm", fn, 3, 4))

    assert registry.finishes == [{"item_id": "item-1", "status": "succeeded", "error": None}]


def test_run_task_sync_in_thread_failure_marks_failed(registry):
    def fn():
        raise RuntimeError("nope")

    asyncio.run(run_task("warm", "startup.task.warm", fn))

    assert registry.finishes == [
        {
            "item_id": "item-1",
            "status": background_work.STATUS_FAILED,
            "error": "nope",
        }
    ]


def test_run_task_sync_inline_success_finishes(registry):
    calls = []

    def fn():
        calls.append("ran")
        return "ignored-payload"

    asyncio.run(run_task("fast", "startup.task.fast", fn, in_thread=False))

    assert calls == ["ran"]
    # The return value is intentionally dropped by the wrapper.
    assert registry.finishes == [{"item_id": "item-1", "status": "succeeded", "error": None}]


def test_run_task_sync_inline_failure_marks_failed(registry):
    def fn():
        raise KeyError("missing")

    asyncio.run(run_task("fast", "startup.task.fast", fn, in_thread=False))

    assert registry.finishes == [
        {
            "item_id": "item-1",
            "status": background_work.STATUS_FAILED,
            "error": "'missing'",
        }
    ]


def test_run_composite_task_success_finishes(registry):
    order = []

    async def body():
        order.append("first")
        await asyncio.sleep(0)
        order.append("second")

    asyncio.run(run_composite_task("compose", "startup.task.compose", body))

    assert order == ["first", "second"]
    assert registry.finishes == [{"item_id": "item-1", "status": "succeeded", "error": None}]


def test_run_composite_task_failure_marks_failed(registry, caplog):
    async def body():
        raise OSError("disk")

    caplog.set_level(logging.ERROR, logger="startup_tasks")
    asyncio.run(run_composite_task("compose", "startup.task.compose", body))

    assert registry.finishes == [
        {
            "item_id": "item-1",
            "status": background_work.STATUS_FAILED,
            "error": "disk",
        }
    ]
    assert any("startup task compose failed" in r.message for r in caplog.records)
