"""Unit tests for ``git_status_cache.GitStatusCache``.

Branch coverage for the TTL cache that the integration-style
``test_git_status_cache.py`` does not exercise in-suite (that file is a
``__main__`` runner, so pytest never imports it). The fetch is isolated by
monkeypatching ``node_op``; no real node RPC is made.

Pins:
  1. ``ttl_seconds`` property.
  2. ``invalidate`` filters: clear-all, node-only, cwd-only, node+cwd, and
     the per-key mismatch ``continue`` branches.
  3. ``get``: TTL hit (with a defensive copy), cache miss + store, non-dict
     result returned but not stored, exception propagated with in-flight
     cleared, single-flight coalescing of concurrent callers.
  4. Stale-while-refresh: an expired hit serves the old value immediately
     while a background refresh runs; a refresh that raises leaves the old
     entry intact (``_store_refresh`` exception branch).
  5. ``warm_recent``: non-primary node and empty cwd are skipped, duplicate
     projects collapse, the startup warm limit caps fetches, a failing
     ``list_projects`` returns silently, and a per-project fetch failure is
     swallowed while warming continues.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_git_status_cache_unit.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys

import _test_home

import pytest

pytestmark = pytest.mark.anyio
_TMP_HOME = _test_home.isolate("bc-test-git-status-cache-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import git_status_cache as gsc  # noqa: E402
from git_status_cache import GitStatusCache  # noqa: E402


def _install_fake_node_op(monkeypatch, fetch):
    """Route every fetch through ``fetch(node_id, cwd)`` and record calls.

    ``fetch`` is async. Returns the recorded ``(node_id, cwd)`` call list.
    """
    calls: list[tuple[str, str]] = []

    async def fake_node_op(node_id, method, params):
        cwd = params.get("cwd", "")
        calls.append((node_id, cwd))
        return await fetch(node_id, cwd)

    monkeypatch.setattr(gsc, "node_op", fake_node_op)
    return calls


def _fetch_returning(value_fn):
    async def fetch(node_id, cwd):
        return value_fn(node_id, cwd)

    return fetch


# --- ttl_seconds -----------------------------------------------------------


def test_ttl_seconds_property() -> None:
    assert GitStatusCache(ttl_seconds=12.5).ttl_seconds == 12.5


# --- invalidate ------------------------------------------------------------


def test_invalidate_clear_all() -> None:
    cache = GitStatusCache()
    cache._entries[("primary", "/a")] = (0.0, {"x": 1})
    cache._entries[("primary", "/b")] = (0.0, {"x": 2})
    cache._entries[("other", "/a")] = (0.0, {"x": 3})

    cache.invalidate()

    assert cache._entries == {}


def test_invalidate_by_node_only() -> None:
    cache = GitStatusCache()
    cache._entries[("primary", "/a")] = (0.0, {})
    cache._entries[("primary", "/b")] = (0.0, {})
    cache._entries[("other", "/a")] = (0.0, {})

    cache.invalidate(node_id="primary")

    assert set(cache._entries) == {("other", "/a")}


def test_invalidate_by_cwd_only() -> None:
    cache = GitStatusCache()
    cache._entries[("primary", "/a")] = (0.0, {})
    cache._entries[("primary", "/b")] = (0.0, {})
    cache._entries[("other", "/a")] = (0.0, {})

    cache.invalidate(cwd="/a")

    assert set(cache._entries) == {("primary", "/b")}


def test_invalidate_by_node_and_cwd() -> None:
    cache = GitStatusCache()
    cache._entries[("primary", "/a")] = (0.0, {})
    cache._entries[("primary", "/b")] = (0.0, {})
    cache._entries[("other", "/a")] = (0.0, {})

    cache.invalidate(node_id="primary", cwd="/a")

    assert set(cache._entries) == {("primary", "/b"), ("other", "/a")}


# --- get -------------------------------------------------------------------


async def test_get_serves_ttl_hit_as_defensive_copy(monkeypatch) -> None:
    calls = _install_fake_node_op(
        monkeypatch, _fetch_returning(lambda n, c: {"branch": c})
    )
    cache = GitStatusCache(ttl_seconds=60.0)

    first = await cache.get("primary", "/repo")
    second = await cache.get("primary", "/repo")

    assert first == {"branch": "/repo"} == second
    assert len(calls) == 1  # second call served from cache
    # The returned dict is a copy: mutating it cannot corrupt the cache.
    second["poison"] = True
    third = await cache.get("primary", "/repo")
    assert "poison" not in third


async def test_get_non_dict_result_returned_but_not_cached(monkeypatch) -> None:
    calls = _install_fake_node_op(
        monkeypatch, _fetch_returning(lambda n, c: "not-a-dict")
    )
    cache = GitStatusCache(ttl_seconds=60.0)

    result = await cache.get("primary", "/repo")
    again = await cache.get("primary", "/repo")

    assert result == "not-a-dict"
    assert again == "not-a-dict"
    assert len(calls) == 2  # never stored, so re-fetched


async def test_get_propagates_exception_and_clears_inflight(monkeypatch) -> None:
    async def boom(node_id, cwd):
        raise RuntimeError("node down")

    _install_fake_node_op(monkeypatch, boom)
    cache = GitStatusCache(ttl_seconds=60.0)

    with pytest.raises(RuntimeError, match="node down"):
        await cache.get("primary", "/repo")

    assert cache._inflight == {}


async def test_concurrent_gets_share_single_fetch(monkeypatch) -> None:
    released = asyncio.Event()

    async def fetch(node_id, cwd):
        await released.wait()
        return {"branch": cwd}

    calls = _install_fake_node_op(monkeypatch, fetch)
    cache = GitStatusCache(ttl_seconds=60.0)

    t1 = asyncio.create_task(cache.get("primary", "/repo"))
    t2 = asyncio.create_task(cache.get("primary", "/repo"))
    await asyncio.sleep(0)
    released.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1 == r2 == {"branch": "/repo"}
    assert len(calls) == 1


# --- stale-while-refresh ---------------------------------------------------


async def test_stale_hit_serves_old_value_while_refresh_runs(monkeypatch) -> None:
    released = asyncio.Event()
    state = {"n": 0}

    async def fetch(node_id, cwd):
        state["n"] += 1
        if state["n"] == 1:
            return {"branch": "old"}
        await released.wait()
        return {"branch": "new"}

    calls = _install_fake_node_op(monkeypatch, fetch)
    cache = GitStatusCache(ttl_seconds=60.0)

    warm = await cache.get("primary", "/repo")
    assert warm == {"branch": "old"}

    # Force the entry past its TTL so the next get treats it as stale,
    # without depending on real-clock granularity.
    key = ("primary", "/repo")
    ts, val = cache._entries[key]
    cache._entries[key] = (ts - cache.ttl_seconds - 1, val)

    # Expired: serve stale "old" immediately and start exactly one refresh.
    stale = await asyncio.wait_for(cache.get("primary", "/repo"), timeout=1.0)
    assert stale == {"branch": "old"}
    # The refresh runs as a background task; yield so it starts and records
    # its fetch call (then suspends on `released`).
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(calls) == 2

    released.set()
    for _ in range(10):
        await asyncio.sleep(0)

    assert cache._entries[key][1] == {"branch": "new"}


async def test_refresh_failure_preserves_cached_entry(monkeypatch) -> None:
    released = asyncio.Event()
    state = {"n": 0}

    async def fetch(node_id, cwd):
        state["n"] += 1
        if state["n"] == 1:
            return {"branch": "old"}
        await released.wait()
        raise RuntimeError("refresh failed")

    calls = _install_fake_node_op(monkeypatch, fetch)
    cache = GitStatusCache(ttl_seconds=60.0)

    await cache.get("primary", "/repo")  # populate "old"

    # Backdate past TTL so the next get is a stale hit that triggers a refresh.
    key = ("primary", "/repo")
    ts, val = cache._entries[key]
    cache._entries[key] = (ts - cache.ttl_seconds - 1, val)

    stale = await asyncio.wait_for(cache.get("primary", "/repo"), timeout=1.0)
    assert stale == {"branch": "old"}
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(calls) == 2

    released.set()
    for _ in range(10):
        await asyncio.sleep(0)

    # _store_refresh swallowed the exception; old entry intact, in-flight cleared.
    assert cache._entries[key][1] == {"branch": "old"}
    assert cache._inflight == {}


async def test_stale_concurrent_get_reuses_inflight_refresh(monkeypatch) -> None:
    released = asyncio.Event()
    state = {"n": 0}

    async def fetch(node_id, cwd):
        state["n"] += 1
        if state["n"] == 1:
            return {"branch": "old"}
        await released.wait()
        return {"branch": "refreshed"}

    calls = _install_fake_node_op(monkeypatch, fetch)
    cache = GitStatusCache(ttl_seconds=60.0)

    await cache.get("primary", "/repo")  # populate "old"
    key = ("primary", "/repo")
    ts, val = cache._entries[key]
    cache._entries[key] = (ts - cache.ttl_seconds - 1, val)

    # First stale get starts a background refresh and returns the stale value.
    first = asyncio.create_task(cache.get("primary", "/repo"))
    for _ in range(10):
        await asyncio.sleep(0)  # let it start the refresh and suspend on `released`
    # A second stale get must REUSE the in-flight refresh, not start its own.
    second = await cache.get("primary", "/repo")
    r1 = await first

    assert r1 == second == {"branch": "old"}
    assert len(calls) == 2  # one populate + one shared refresh

    released.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert cache._entries[key][1] == {"branch": "refreshed"}


async def test_store_refresh_keeps_newer_inflight_task() -> None:
    """``_store_refresh`` must not evict an in-flight task that a newer fetch
    replaced — it pops only its own task. Covers the guard's False branch."""
    cache = GitStatusCache(ttl_seconds=60.0)
    key = ("primary", "/repo")

    async def returns_dict():
        return {"branch": "fresh"}

    completed = asyncio.create_task(returns_dict())
    await completed  # finish the refresh task before handing it in

    # Simulate a newer fetch having taken the in-flight slot.
    newer = asyncio.create_task(asyncio.sleep(3600))
    cache._inflight[key] = newer

    await cache._store_refresh(key, completed)

    # Result stored, but the newer in-flight task was left in place.
    assert cache._entries[key][1] == {"branch": "fresh"}
    assert cache._inflight[key] is newer

    newer.cancel()
    try:
        await newer
    except asyncio.CancelledError:
        pass


# --- warm_recent -----------------------------------------------------------


async def test_warm_recent_filters_and_primes(monkeypatch) -> None:
    import project_store

    projects = [
        {"node_id": "primary", "path": "/a"},  # warmed
        {"node_id": "other", "path": "/b"},  # skipped: non-primary node
        {"node_id": "primary", "path": ""},  # skipped: empty cwd
        {"node_id": "primary", "path": "/a"},  # skipped: duplicate of /a
        {"node_id": "primary", "path": "/c"},  # warmed
    ]
    monkeypatch.setattr(project_store, "list_projects", lambda: projects)
    calls = _install_fake_node_op(
        monkeypatch, _fetch_returning(lambda n, c: {"branch": c})
    )
    cache = GitStatusCache(ttl_seconds=60.0)

    await cache.warm_recent()

    assert sorted(cwd for _, cwd in calls) == ["/a", "/c"]


async def test_warm_recent_respects_startup_limit(monkeypatch) -> None:
    import project_store

    projects = [{"node_id": "primary", "path": f"/r{i}"} for i in range(5)]
    monkeypatch.setattr(project_store, "list_projects", lambda: projects)
    calls = _install_fake_node_op(
        monkeypatch, _fetch_returning(lambda n, c: {"b": c})
    )
    cache = GitStatusCache(ttl_seconds=60.0, startup_warm_limit=2)

    await cache.warm_recent()

    assert len(calls) == 2


async def test_warm_recent_swallows_project_list_failure(monkeypatch) -> None:
    import project_store

    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(project_store, "list_projects", boom)
    calls = _install_fake_node_op(monkeypatch, _fetch_returning(lambda n, c: {}))
    cache = GitStatusCache(ttl_seconds=60.0)

    await cache.warm_recent()  # must not raise

    assert calls == []


async def test_warm_recent_swallows_per_project_fetch_failure(monkeypatch) -> None:
    import project_store

    monkeypatch.setattr(
        project_store,
        "list_projects",
        lambda: [
            {"node_id": "primary", "path": "/boom"},
            {"node_id": "primary", "path": "/ok"},
        ],
    )

    async def fetch(node_id, cwd):
        if cwd == "/boom":
            raise RuntimeError("per-project fail")
        return {"ok": True}

    calls = _install_fake_node_op(monkeypatch, fetch)
    cache = GitStatusCache(ttl_seconds=60.0)

    await cache.warm_recent()  # must not raise; continues to /ok

    assert sorted(cwd for _, cwd in calls) == ["/boom", "/ok"]


if __name__ == "__main__":
    import shutil

    import pytest as _pytest

    try:
        _rc = _pytest.main([__file__, "-v"])
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(_rc)
