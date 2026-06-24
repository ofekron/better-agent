"""Locks the `Coordinator._cancelled_ids` init contract.

`_cancelled_ids` is initialized in `__init__` alongside every other
per-session dict, so all access sites (`cancel_queued`,
`_run_session_processor`, `cancel_session`) read/write it directly
without defensive `hasattr`/`getattr` guards. This regression-locks
the originally-audited defect where the attribute was created lazily
via `hasattr` inside `cancel_queued` — making access-site guards a
load-bearing contract instead of a redundancy.

Run with:
    cd backend && .venv/bin/python scripts/test_cancelled_ids_initialized_in_init.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cancelled-ids-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    coord = Coordinator()

    # Control: a sibling per-session dict IS initialized in __init__.
    results.append((
        "control: _queued_ids initialized in __init__",
        hasattr(coord, "_queued_ids"),
        "missing — test harness assumption broken",
    ))

    # The contract: _cancelled_ids exists right after construction, so
    # access sites need no defensive guard.
    results.append((
        "_cancelled_ids initialized in __init__ (not lazy hasattr)",
        hasattr(coord, "_cancelled_ids"),
        "absent right after construction — created lazily in cancel_queued",
    ))

    # And the cancel path populates it via direct setdefault (no guard).
    coord._queued_ids["sess-x"] = ["q1", "q2"]
    coord.cancel_queued("sess-x")
    results.append((
        "cancel_queued records cancelled queued ids",
        coord._cancelled_ids.get("sess-x") == {"q1", "q2"},
        f"got {coord._cancelled_ids.get('sess-x')!r}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    # The middle check is EXPECTED to fail on buggy code; the suite
    # passes only when __init__ initializes the attribute.
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
