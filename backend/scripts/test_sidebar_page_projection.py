#!/usr/bin/env python3
import os
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


HOME = tempfile.TemporaryDirectory(prefix="better-agent-sidebar-page-")
os.environ["BETTER_AGENT_HOME"] = HOME.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_store


def _summary(index: int) -> dict:
    return {
        "id": f"session-{index:05d}",
        "name": f"Session {index}",
        "updated_at": f"2026-07-12T12:{index % 60:02d}:{index % 60:02d}+00:00",
        "last_user_prompt_at": None,
        "last_opened_at": None,
        "pinned": index % 101 == 0,
        "archived": index % 97 == 0,
        "working_mode": None,
        "working_mode_meta": None,
        "cwd": "/project/a" if index % 2 else "/project/b",
        "all_projects": index % 997 == 0,
        "messages": [],
    }


def _seed(count: int = 20_000) -> None:
    with session_store._summary_index_lock:
        session_store._summary_index.clear()
        session_store._summary_index.update(
            (summary["id"], summary)
            for summary in (_summary(index) for index in range(count))
        )
        session_store._summary_index_loaded = True
        session_store._summary_index_version += 1
        session_store._summary_order_version += 1
        session_store._summary_visibility_version += 1
        session_store._summary_sorted_id_caches.clear()
        session_store._sidebar_page_projections.clear()


def test_visibility_contract() -> None:
    base = _summary(1)
    non_visibility = dict(base, name="renamed", provider_id="codex")
    assert not session_store._summary_visibility_changed(base, non_visibility)
    for changed in (
        dict(base, archived=True),
        dict(base, working_mode="prompt_engineering"),
        dict(base, working_mode="file_editing", working_mode_meta={"persistent": True}),
        dict(base, cwd="/elsewhere"),
        dict(base, all_projects=True),
    ):
        assert session_store._summary_visibility_changed(base, changed)


def test_atomic_page_and_warm_concurrency() -> None:
    _seed()
    original_reconcile = session_store._reconcile_summary_index_roots
    session_store._reconcile_summary_index_roots = lambda: (_ for _ in ()).throw(
        AssertionError("request path enumerated session roots")
    )
    try:
        page, total, order_generation, visibility_generation = (
            session_store.sidebar_session_summary_page(
                "updated_at", "/project/a", 0, 100,
            )
        )
        assert len(page) == 100
        assert total > 9_000
        assert order_generation == session_store._summary_order_version
        assert visibility_generation == session_store._summary_visibility_version
        assert all(
            item.get("all_projects") or item.get("cwd") == "/project/a"
            for item in page
        )
        page[0]["name"] = "mutated"
        assert session_store._summary_index[page[0]["id"]]["name"] != "mutated"

        def request() -> float:
            started = time.perf_counter()
            result, result_total, _, _ = session_store.sidebar_session_summary_page(
                "updated_at", "/project/a", 100, 100,
            )
            assert len(result) == 100 and result_total == total
            return (time.perf_counter() - started) * 1000.0

        with ThreadPoolExecutor(max_workers=32) as pool:
            timings = list(pool.map(lambda _: request(), range(64)))
        p95 = statistics.quantiles(timings, n=20)[18]
        assert p95 < 200.0, (p95, max(timings))
    finally:
        session_store._reconcile_summary_index_roots = original_reconcile


def test_projection_invalidation() -> None:
    _seed(100)
    _, total_before, _, visibility_before = session_store.sidebar_session_summary_page(
        "updated_at", None, 0, 100,
    )
    sid = "session-00001"
    with session_store._summary_index_lock:
        before = session_store._summary_index[sid]
        after = dict(before, archived=True)
        session_store._summary_index[sid] = after
        if session_store._summary_visibility_changed(before, after):
            session_store._summary_visibility_version += 1
    _, total_after, _, visibility_after = session_store.sidebar_session_summary_page(
        "updated_at", None, 0, 100,
    )
    assert total_after == total_before - 1
    assert visibility_after == visibility_before + 1


if __name__ == "__main__":
    try:
        test_visibility_contract()
        test_atomic_page_and_warm_concurrency()
        test_projection_invalidation()
        print("PASS: sidebar page projection")
    finally:
        HOME.cleanup()
