from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-ingester-cache-budget-")

import event_ingester as event_ingester_mod  # noqa: E402
from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _payload(uid: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x" * 900}]},
    }


def _warm_root(ingester: EventIngester, root_id: str) -> int:
    ingester.ingest(
        root_id,
        sid=root_id,
        event_type="agent_message",
        data=_payload(f"{root_id}-event"),
        source="test",
        msg_id=f"{root_id}-msg",
    )
    rows, total, has_more = ingester.read_events(root_id, limit=10)
    assert len(rows) == 1 and total == 1 and not has_more
    return ingester._events_path(root_id).stat().st_size


def _run() -> bool:
    original_budget = event_ingester_mod._FULL_SCAN_CACHE_MAX_BYTES
    results: list[tuple[str, bool, str]] = []
    try:
        ingester = EventIngester()
        first_size = _warm_root(ingester, "root-cache-budget-a")
        event_ingester_mod._FULL_SCAN_CACHE_MAX_BYTES = first_size + 512
        _warm_root(ingester, "root-cache-budget-b")

        results.append((
            "full-scan cache evicts the older root",
            "root-cache-budget-a" not in ingester._full_scan_cache
            and "root-cache-budget-b" in ingester._full_scan_cache,
            f"cached={list(ingester._full_scan_cache)}",
        ))
        results.append((
            "full-scan cache byte accounting stays within budget",
            ingester._full_scan_cache_total_bytes <= event_ingester_mod._FULL_SCAN_CACHE_MAX_BYTES,
            (
                f"bytes={ingester._full_scan_cache_total_bytes} "
                f"budget={event_ingester_mod._FULL_SCAN_CACHE_MAX_BYTES}"
            ),
        ))

        oversized = EventIngester()
        event_ingester_mod._FULL_SCAN_CACHE_MAX_BYTES = 1
        _warm_root(oversized, "root-cache-budget-oversized")
        results.append((
            "oversized single-root full scan is not retained",
            oversized._full_scan_cache == {}
            and oversized._full_scan_cache_total_bytes == 0,
            f"cached={list(oversized._full_scan_cache)} bytes={oversized._full_scan_cache_total_bytes}",
        ))
    finally:
        event_ingester_mod._FULL_SCAN_CACHE_MAX_BYTES = original_budget

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
