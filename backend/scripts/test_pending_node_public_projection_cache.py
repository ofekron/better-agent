from __future__ import annotations

import os
import shutil
import sys

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
    first = node_link.public_pending_nodes()
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
        first == []
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


if __name__ == "__main__":
    try:
        sys.exit(0 if test_public_projection_cache_invalidates_on_mutation() else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
