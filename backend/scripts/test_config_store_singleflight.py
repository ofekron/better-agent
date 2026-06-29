"""Regression: config_store._load_state must single-flight its cold disk
read under _state_cache_lock.

The faulthandler watchdog ranked `config_store._load_state -> read_json` the
#2 event-loop blocker (137 dumps; 120 in a single restart hour). Root cause:
the disk read sat OUTSIDE the cache lock, so a restart-time thundering herd of
concurrent first-access callers each ran the synchronous read on the loop.

This test pins the fix: a concurrent cold-cache herd performs exactly ONE
read_json, every caller observes identical state, and a fingerprint change
(config rewritten) triggers exactly one additional read.
"""
from __future__ import annotations

import os
import json
import sys
import threading
import time
import tempfile

import _test_home
_test_home.isolate("bc-test-config-singleflight-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402


def _reset_state_cache() -> None:
    with config_store._state_cache_lock:
        config_store._state_cache = None


def _seed_config(providers) -> None:
    path = config_store._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"default_provider_id": None, "providers": providers}),
        encoding="utf-8",
    )


def test_cold_cache_herd_reads_disk_once() -> None:
    _seed_config([{"id": "p1", "kind": "claude", "mode": "subscription"}])
    _reset_state_cache()

    real_read_json = config_store.read_json
    reads = {"n": 0}
    reads_lock = threading.Lock()

    def counting_read_json(path, default):
        with reads_lock:
            reads["n"] += 1
        # Widen the window where a naive (outside-lock) implementation would
        # let the herd pile onto disk concurrently.
        time.sleep(0.02)
        return real_read_json(path, default)

    config_store.read_json = counting_read_json
    try:
        results = [None] * 24
        barrier = threading.Barrier(len(results))

        def worker(i: int) -> None:
            barrier.wait()  # release all threads at the cold cache together
            results[i] = config_store._load_state()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(results))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        config_store.read_json = real_read_json

    assert reads["n"] == 1, f"cold-cache herd must read disk once, got {reads['n']}"
    ids = {tuple(p.get("id") for p in (r or {}).get("providers", [])) for r in results}
    assert ids == {("p1",)}, f"all callers must see identical state, got {ids}"


def test_fingerprint_change_triggers_one_reload() -> None:
    _seed_config([{"id": "p1", "kind": "claude", "mode": "subscription"}])
    _reset_state_cache()

    real_read_json = config_store.read_json
    reads = {"n": 0}

    def counting_read_json(path, default):
        reads["n"] += 1
        return real_read_json(path, default)

    config_store.read_json = counting_read_json
    try:
        first = config_store._load_state()
        assert [p["id"] for p in first["providers"]] == ["p1"]
        assert reads["n"] == 1

        # Warm hit: no new read.
        config_store._load_state()
        assert reads["n"] == 1, "warm cache must not touch disk"

        # Rewrite config so mtime_ns/size fingerprint changes; ensure the
        # stat resolution actually advances even on coarse clocks.
        time.sleep(0.01)
        _seed_config([
            {"id": "p1", "kind": "claude", "mode": "subscription"},
            {"id": "p2", "kind": "claude", "mode": "subscription"},
        ])
        second = config_store._load_state()
        assert [p["id"] for p in second["providers"]] == ["p1", "p2"]
        assert reads["n"] == 2, f"fingerprint change must reload exactly once, got {reads['n']}"
    finally:
        config_store.read_json = real_read_json


if __name__ == "__main__":
    test_cold_cache_herd_reads_disk_once()
    test_fingerprint_change_triggers_one_reload()
    print("PASS config_store single-flight")
