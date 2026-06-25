"""Regression: append-handle prune must not abandon the whole cache when the
LRU-oldest root's lock is contended.

Pre-fix `_prune_append_handles` did `return` the instant the oldest victim's
per-root lock couldn't be acquired non-blocking, leaving the cache above the
cap. Under sustained write contention on the oldest handle the cache grew
past `_MAX_OPEN_APPEND_HANDLES` unbounded — an fd leak feeding the Errno 24
exhaustion. Post-fix it skips the contended victim and prunes the
next-oldest, converging the cache back to the cap.
"""
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "backend" / "scripts"))

import _test_home  # noqa: E402
_test_home.isolate("bc-prune-fd-")

import event_ingester  # noqa: E402
from event_ingester import EventIngester  # noqa: E402


def test_prune_skips_contended_victim():
    cap = 2
    event_ingester._MAX_OPEN_APPEND_HANDLES = cap
    ing = EventIngester()
    tmp = Path(tempfile.mkdtemp(prefix="prune-fd-"))
    roots = [f"root{i}" for i in range(5)]  # root0 = LRU-oldest
    for r in roots:
        p = tmp / f"{r}.jsonl"
        fh = open(p, "a", encoding="utf-8")
        ing._handles[r] = (p, fh)
        ing._locks[r] = threading.Lock()

    oldest = roots[0]
    assert ing._locks[oldest].acquire(blocking=False)  # simulate concurrent write
    try:
        # exclude a root NOT in the cache so nothing is spared from pruning;
        # the only thing sparing a victim is a held lock.
        ing._prune_append_handles(exclude_root_id="not-a-cached-root")
    finally:
        ing._locks[oldest].release()

    # Post-fix: cache converged back to the cap. Pre-fix it stayed at 5.
    assert len(ing._handles) <= cap, (
        f"cache not pruned to cap {cap}: {list(ing._handles)}"
    )
    # The contended oldest could not be closed, so it must have survived.
    assert oldest in ing._handles

    for _r, (_p, fh) in list(ing._handles.items()):
        fh.close()


def _run():
    test_prune_skips_contended_victim()
    print("ok  test_prune_skips_contended_victim")
    print("PASS (1 test)")


if __name__ == "__main__":
    _run()
