from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-pending-node-cache-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import node_link  # noqa: E402
from stores import pending_node_registrations  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_public_projection_cache_invalidates_on_mutation() -> bool:
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
    return ok


def test_public_projection_cache_invalidates_on_expiry() -> bool:
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
    return ok


if __name__ == "__main__":
    try:
        ok = (
            test_public_projection_cache_invalidates_on_mutation()
            and test_public_projection_cache_invalidates_on_expiry()
        )
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
