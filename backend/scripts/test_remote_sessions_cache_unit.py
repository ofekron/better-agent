#!/usr/bin/env python3
"""Dedicated unit coverage for backend/remote_sessions_cache.py.

remote_sessions_cache.py is the per-node TTL cache over each remote node's
session list: it owns the entry map, the lock, the in-flight refresh set, and
the version counter downstream response caches key off. A stale entry is
served immediately while a single background refresh repopulates it; only a
genuinely changed list bumps the version.

The only same-name owner, test_remote_sessions_cache.py, is a standalone
__main__ script (pytest collects 0 items). The other importers
(test_session_list_pagination, test_session_listing_page_scope) touch the
module only incidentally via the live sidebar flow and measured ~28% line / 0%
branch. This file drives every callable + branch hermetically. The module is
pure in-memory + an asyncio node_link RPC; no bc_home disk I/O, but
engage_test_home is applied for convention. No real state is ever touched.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

import pytest

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-rsc-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(str(_TEST_HOME))

import remote_sessions_cache as rsc  # noqa: E402


# --- fakes / helpers ---------------------------------------------------------


class _FakePerf:
    """Records perf.record(name, value) calls so metric branches are asserted."""

    def __init__(self) -> None:
        self.records: list[tuple[str, float]] = []

    def record(self, name: str, value: float) -> None:
        self.records.append((name, value))

    def names(self) -> list[str]:
        return [n for n, _ in self.records]


def _install_fake_perf(monkeypatch) -> _FakePerf:
    fake = _FakePerf()
    monkeypatch.setattr(rsc, "perf", fake)
    return fake


def _install_node_link(monkeypatch, rpc_call) -> types.ModuleType:
    """Inject a fake node_link module (fetch_live late-imports it per call)."""
    mod = types.ModuleType("node_link")
    mod.rpc_call = rpc_call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "node_link", mod)
    return mod


def _patch_create_task_capture(monkeypatch) -> list[asyncio.Task]:
    """Capture every asyncio.create_task so spawned refresh tasks can be awaited."""
    captured: list[asyncio.Task] = []
    orig = asyncio.create_task

    def _capture(coro):
        task = orig(coro)
        captured.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", _capture)
    return captured


def _stale_entry(cache: rsc.RemoteSessionsCache, node_id: str, sessions: list[dict]) -> None:
    """Push an entry then rewind its timestamp so get() reports it as stale."""
    cache.put(node_id, sessions)
    ts, cleaned = cache._entries[node_id]
    cache._entries[node_id] = (ts - 1000.0, cleaned)


# --- copy_sessions -----------------------------------------------------------


def test_copy_sessions_empty_returns_empty_list():
    assert rsc.copy_sessions([]) == []


def test_copy_sessions_filters_non_dict_items():
    src = [{"a": 1}, "x", None, {"b": 2}, 5]
    out = rsc.copy_sessions(src)
    assert out == [{"a": 1}, {"b": 2}]


def test_copy_sessions_returns_shallow_copies_not_references():
    original = [{"id": "s1"}]
    out = rsc.copy_sessions(original)
    assert out == original
    assert out[0] is not original[0]
    out[0]["mutated"] = True
    assert "mutated" not in original[0]


def test_copy_sessions_limit_truncates_and_breaks_early():
    src = [{"i": i} for i in range(5)]
    out = rsc.copy_sessions(src, limit=2)
    assert out == [{"i": 0}, {"i": 1}]


def test_copy_sessions_limit_none_returns_all():
    src = [{"i": i} for i in range(3)]
    assert len(rsc.copy_sessions(src, limit=None)) == 3


def test_copy_sessions_limit_at_or_above_count_returns_all():
    src = [{"i": i} for i in range(3)]
    assert len(rsc.copy_sessions(src, limit=3)) == 3
    assert len(rsc.copy_sessions(src, limit=10)) == 3


# --- init / properties -------------------------------------------------------


def test_init_defaults():
    cache = rsc.RemoteSessionsCache()
    assert cache.ttl_seconds == 2.0
    assert cache.fetch_timeout_seconds == 0.75
    assert cache.version() == 0
    assert cache.get("any") == (None, False, 0)


def test_init_custom_values():
    cache = rsc.RemoteSessionsCache(ttl_seconds=5.0, fetch_timeout_seconds=2.5)
    assert cache.ttl_seconds == 5.0
    assert cache.fetch_timeout_seconds == 2.5


# --- get / put / version / clear --------------------------------------------


def test_put_new_entry_bumps_version():
    cache = rsc.RemoteSessionsCache()
    v0 = cache.version()
    cache.put("n1", [{"id": "a"}])
    assert cache.version() == v0 + 1


def test_put_changed_list_bumps_version_again():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}])
    v = cache.version()
    cache.put("n1", [{"id": "b"}])
    assert cache.version() == v + 1


def test_put_identical_list_does_not_bump_version_but_refreshes_timestamp():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}])
    v = cache.version()
    ts_before = cache._entries["n1"][0]
    # Identical content (different dict identity) -> early return, no bump.
    cache.put("n1", [{"id": "a"}])
    assert cache.version() == v
    assert cache._entries["n1"][0] >= ts_before


def test_put_filters_non_dict_items_before_storing():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}, "junk", 7])
    cached, fresh, total = cache.get("n1")
    assert cached == [{"id": "a"}]
    assert total == 1
    assert fresh is True


def test_put_stores_defensive_copy():
    cache = rsc.RemoteSessionsCache()
    payload = [{"id": "a"}]
    cache.put("n1", payload)
    payload.append({"id": "evil"})
    cached, _, _ = cache.get("n1")
    assert cached == [{"id": "a"}]


def test_get_miss_returns_none_tuple():
    cache = rsc.RemoteSessionsCache()
    assert cache.get("missing") == (None, False, 0)


def test_get_fresh_hit_returns_copy_total_and_fresh_flag():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}, {"id": "b"}])
    cached, fresh, total = cache.get("n1")
    assert cached == [{"id": "a"}, {"id": "b"}]
    assert fresh is True
    assert total == 2


def test_get_stale_hit_returns_copy_with_fresh_false():
    cache = rsc.RemoteSessionsCache(ttl_seconds=2.0)
    _stale_entry(cache, "n1", [{"id": "a"}])
    cached, fresh, total = cache.get("n1")
    assert cached == [{"id": "a"}]
    assert fresh is False
    assert total == 1


def test_get_limit_applies_to_copy_not_total():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"i": i} for i in range(4)])
    cached, fresh, total = cache.get("n1", limit=2)
    assert len(cached) == 2
    assert total == 4
    assert fresh is True


def test_get_returns_copies_not_cached_references():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}])
    cached, _, _ = cache.get("n1")
    cached[0]["mutated"] = True
    again, _, _ = cache.get("n1")
    assert "mutated" not in again[0]


def test_clear_resets_entries_and_version():
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}])
    assert cache.version() == 1
    cache.clear()
    assert cache.version() == 0
    assert cache.get("n1") == (None, False, 0)


# --- fetch_live (async) ------------------------------------------------------


def test_fetch_live_happy_returns_filtered_copy(monkeypatch):
    cache = rsc.RemoteSessionsCache(fetch_timeout_seconds=1.25)
    seen = {}

    async def rpc(node_id, method, payload, *, timeout):
        seen.update(node_id=node_id, method=method, payload=payload, timeout=timeout)
        return {"sessions": [{"id": "a"}, "x", {"id": "b"}]}

    _install_node_link(monkeypatch, rpc)

    out = asyncio.run(cache.fetch_live("n1"))
    assert out == [{"id": "a"}, {"id": "b"}]
    assert seen == {"node_id": "n1", "method": "list_sessions", "payload": {}, "timeout": 1.25}


def test_fetch_live_none_response_returns_empty(monkeypatch):
    async def rpc(*a, **k):
        return None

    _install_node_link(monkeypatch, rpc)
    assert asyncio.run(rsc.RemoteSessionsCache().fetch_live("n1")) == []


def test_fetch_live_missing_sessions_key_returns_empty(monkeypatch):
    async def rpc(*a, **k):
        return {"other": 1}

    _install_node_link(monkeypatch, rpc)
    assert asyncio.run(rsc.RemoteSessionsCache().fetch_live("n1")) == []


def test_fetch_live_non_list_sessions_returns_empty(monkeypatch):
    async def rpc(*a, **k):
        return {"sessions": "not-a-list"}

    _install_node_link(monkeypatch, rpc)
    assert asyncio.run(rsc.RemoteSessionsCache().fetch_live("n1")) == []


# --- schedule_refresh --------------------------------------------------------


def test_schedule_refresh_dedupes_while_inflight(monkeypatch):
    cache = rsc.RemoteSessionsCache()
    block = asyncio.Event()

    async def rpc(node_id, method, payload, *, timeout):
        await block.wait()
        return {"sessions": [{"id": "a"}]}

    _install_node_link(monkeypatch, rpc)
    captured = _patch_create_task_capture(monkeypatch)

    async def driver():
        cache.schedule_refresh("n1")
        cache.schedule_refresh("n1")  # in-flight -> deduped, no second task
        await asyncio.sleep(0)
        assert len(captured) == 1
        block.set()
        await asyncio.gather(*captured)

    asyncio.run(driver())

    assert cache.version() == 1
    assert "n1" not in cache._refreshing


def test_schedule_refresh_populates_cache_on_success(monkeypatch):
    cache = rsc.RemoteSessionsCache()
    block = asyncio.Event()

    async def rpc(node_id, method, payload, *, timeout):
        await block.wait()
        return {"sessions": [{"id": "fresh"}]}

    _install_node_link(monkeypatch, rpc)
    captured = _patch_create_task_capture(monkeypatch)

    async def driver():
        cache.schedule_refresh("n1")
        block.set()
        await asyncio.gather(*captured)

    asyncio.run(driver())

    cached, fresh, total = cache.get("n1")
    assert cached == [{"id": "fresh"}]
    assert total == 1
    assert fresh is True
    assert "n1" not in cache._refreshing


def test_schedule_refresh_swallows_fetch_failure_and_releases_slot(monkeypatch):
    cache = rsc.RemoteSessionsCache()

    async def rpc(*a, **k):
        raise RuntimeError("net down")

    _install_node_link(monkeypatch, rpc)
    captured = _patch_create_task_capture(monkeypatch)

    async def driver():
        cache.schedule_refresh("n1")
        await asyncio.gather(*captured)

    asyncio.run(driver())

    # Failure swallowed: cache untouched, slot released.
    assert cache.version() == 0
    assert cache.get("n1") == (None, False, 0)
    assert "n1" not in cache._refreshing


# --- for_sidebar (async) -----------------------------------------------------


def test_for_sidebar_fresh_hit_records_hit_metric(monkeypatch):
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}])
    perf = _install_fake_perf(monkeypatch)

    out = asyncio.run(cache.for_sidebar("n1"))
    assert out == [{"id": "a"}]
    assert perf.names() == ["sessions.list.remote_cache.hit"]


def test_for_sidebar_stale_hit_serves_cached_and_schedules_refresh(monkeypatch):
    cache = rsc.RemoteSessionsCache()

    async def rpc(node_id, method, payload, *, timeout):
        return {"sessions": [{"id": "refreshed"}]}

    _install_node_link(monkeypatch, rpc)
    perf = _install_fake_perf(monkeypatch)
    captured = _patch_create_task_capture(monkeypatch)

    _stale_entry(cache, "n1", [{"id": "stale"}])

    async def driver():
        out = await cache.for_sidebar("n1")
        assert out == [{"id": "stale"}]
        await asyncio.gather(*captured)
        return None

    asyncio.run(driver())

    assert perf.names() == ["sessions.list.remote_cache.stale"]
    # Background refresh repopulated with the new list and bumped version.
    cached, _, _ = cache.get("n1")
    assert cached == [{"id": "refreshed"}]


def test_for_sidebar_miss_fetches_puts_and_returns(monkeypatch):
    cache = rsc.RemoteSessionsCache()

    async def rpc(node_id, method, payload, *, timeout):
        return {"sessions": [{"id": "live"}]}

    _install_node_link(monkeypatch, rpc)
    perf = _install_fake_perf(monkeypatch)

    out = asyncio.run(cache.for_sidebar("n1"))
    assert out == [{"id": "live"}]
    assert perf.names() == ["sessions.list.remote_cache.miss"]
    cached, _, total = cache.get("n1")
    assert cached == [{"id": "live"}]
    assert total == 1


# --- for_sidebar_cached ------------------------------------------------------


def test_for_sidebar_cached_miss_schedules_refresh_and_returns_none(monkeypatch):
    cache = rsc.RemoteSessionsCache()

    async def rpc(node_id, method, payload, *, timeout):
        return {"sessions": [{"id": "live"}]}

    _install_node_link(monkeypatch, rpc)
    perf = _install_fake_perf(monkeypatch)
    captured = _patch_create_task_capture(monkeypatch)

    async def driver():
        result = cache.for_sidebar_cached("n1")
        assert result is None
        await asyncio.gather(*captured)

    asyncio.run(driver())

    assert perf.names() == ["sessions.list.remote_cache.deferred_miss"]
    cached, _, _ = cache.get("n1")
    assert cached == [{"id": "live"}]


def test_for_sidebar_cached_fresh_records_hit(monkeypatch):
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"id": "a"}, {"id": "b"}])
    perf = _install_fake_perf(monkeypatch)

    cached, total = cache.for_sidebar_cached("n1")
    assert cached == [{"id": "a"}, {"id": "b"}]
    assert total == 2
    assert perf.names() == ["sessions.list.remote_cache.deferred_hit"]


def test_for_sidebar_cached_stale_schedules_refresh_records_stale(monkeypatch):
    cache = rsc.RemoteSessionsCache()

    async def rpc(node_id, method, payload, *, timeout):
        return {"sessions": [{"id": "refreshed"}]}

    _install_node_link(monkeypatch, rpc)
    perf = _install_fake_perf(monkeypatch)
    captured = _patch_create_task_capture(monkeypatch)

    _stale_entry(cache, "n1", [{"id": "stale"}])

    async def driver():
        cached, total = cache.for_sidebar_cached("n1")
        assert cached == [{"id": "stale"}]
        assert total == 1
        await asyncio.gather(*captured)

    asyncio.run(driver())

    assert perf.names() == ["sessions.list.remote_cache.deferred_stale"]
    refreshed, _, _ = cache.get("n1")
    assert refreshed == [{"id": "refreshed"}]


def test_for_sidebar_cached_limit_applies_to_copy(monkeypatch):
    cache = rsc.RemoteSessionsCache()
    cache.put("n1", [{"i": i} for i in range(4)])
    _install_fake_perf(monkeypatch)

    cached, total = cache.for_sidebar_cached("n1", limit=2)
    assert len(cached) == 2
    assert total == 4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
