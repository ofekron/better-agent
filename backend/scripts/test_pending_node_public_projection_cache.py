from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TEST_HOME = _test_home.TestHome.acquire("bc-test-pending-node-cache-")

import node_link  # noqa: E402
from stores import pending_node_registrations  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def setup_function(_function) -> None:
    _test_home.engage(_TEST_HOME.path)


def test_public_projection_cache_invalidates_on_mutation() -> None:
    cold = node_link.public_pending_nodes_cached()
    first = node_link.public_pending_nodes()
    warm = node_link.public_pending_nodes_cached()
    pending_node_registrations.create(
        node_id="worker-a",
        address="127.0.0.1:9000",
        cwd_roots=["/tmp/project"],
        secret_hash="must-not-leak",
        fingerprint="abc123",
    )
    second = node_link.public_pending_nodes()
    third = node_link.public_pending_nodes()
    pending_node_registrations.approve("worker-a")
    fourth = node_link.public_pending_nodes()
    ok = (
        cold is None
        and first == []
        and warm == []
        and len(second) == 1
        and second == third
        and second[0].get("node_id") == "worker-a"
        and second[0].get("fingerprint") == "abc123"
        and "secret_hash" not in second[0]
        and fourth == []
    )
    print(
        f"{PASS if ok else FAIL} pending-node public projection cache "
        f"-- first={first} second={second} fourth={fourth}",
    )
    assert ok


def test_public_projection_cache_invalidates_on_expiry() -> None:
    pending_node_registrations.create(
        node_id="worker-expired",
        address="127.0.0.1:9001",
        cwd_roots=["/tmp/expired"],
        secret_hash="must-not-leak-expired",
        fingerprint="expired123",
    )
    warm = node_link.public_pending_nodes()
    rec = pending_node_registrations.get("worker-expired")
    assert rec is not None
    expired_at = (datetime.now() - timedelta(seconds=1)).isoformat()
    rec["expires_at"] = expired_at
    pending_node_registrations._path("worker-expired").write_text(json.dumps(rec), encoding="utf-8")
    pending_node_registrations._cache_loaded = False
    node_link._public_pending_cache = (
        pending_node_registrations.version(),
        [{**warm[0], "expires_at": expired_at}],
    )
    cached = node_link.public_pending_nodes_cached()
    after_expiry = node_link.public_pending_nodes()
    ok = (
        len(warm) == 1
        and cached is None
        and after_expiry == []
    )
    print(
        f"{PASS if ok else FAIL} pending-node public projection cache expiry "
        f"-- warm={warm} cached={cached} after={after_expiry}",
    )
    assert ok


def test_pending_store_cache_is_scoped_to_state_home() -> None:
    home_a = _test_home.TestHome.acquire("bc-pending-node-home-a-")
    home_b = _test_home.TestHome.acquire("bc-pending-node-home-b-")
    try:
        _test_home.engage(home_a.path)
        pending_node_registrations.create(
            node_id="home-a-node",
            address="127.0.0.1:9101",
            cwd_roots=[home_a.path],
            secret_hash="home-a-secret",
            fingerprint="home-a",
        )
        assert [r["node_id"] for r in pending_node_registrations.list_pending()] == [
            "home-a-node"
        ]

        _test_home.engage(home_b.path)
        assert pending_node_registrations.list_pending() == []
        pending_node_registrations.create(
            node_id="home-b-node",
            address="127.0.0.1:9102",
            cwd_roots=[home_b.path],
            secret_hash="home-b-secret",
            fingerprint="home-b",
        )
        assert [r["node_id"] for r in pending_node_registrations.list_pending()] == [
            "home-b-node"
        ]

        _test_home.engage(home_a.path)
        assert [r["node_id"] for r in pending_node_registrations.list_pending()] == [
            "home-a-node"
        ]
    finally:
        _test_home.engage(_TEST_HOME.path)
        home_b.release()
        home_a.release()


def teardown_module() -> None:
    _TEST_HOME.release()


if __name__ == "__main__":
    try:
        test_public_projection_cache_invalidates_on_mutation()
        test_public_projection_cache_invalidates_on_expiry()
        test_pending_store_cache_is_scoped_to_state_home()
    finally:
        teardown_module()
