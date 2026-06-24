"""Regression: brand-new empty (0-message) sessions sort first in the list.

The first order field is `message_count == 0`, so a new empty session
outranks even a pinned, recently-updated non-empty session.

Run with:
    cd backend && .venv/bin/python scripts/test_session_list_empty_first.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-empty-first-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def main() -> int:
    try:
        import main as app_main  # noqa: E402

        empty_new = {
            "id": "empty-new",
            "updated_at": "2026-06-01T00:00:00",
            "pinned": False,
            "message_count": 0,
        }
        busy_pinned = {
            "id": "busy-pinned",
            "updated_at": "2026-06-20T00:00:00",
            "pinned": True,
            "message_count": 12,
        }
        busy_recent = {
            "id": "busy-recent",
            "updated_at": "2026-06-25T00:00:00",
            "pinned": False,
            "message_count": 3,
        }

        order = [
            s["id"]
            for s in sorted(
                [busy_pinned, busy_recent, empty_new],
                key=lambda s: app_main._session_list_sort_key(
                    s, False, "updated_at"
                ),
                reverse=True,
            )
        ]
        ok = order[0] == "empty-new"
        print(
            f"{PASS if ok else FAIL} empty 0-message session sorts first"
            f"{'' if ok else f' — got {order}'}"
        )

        # Among empty sessions, pinned + recency still break the tie.
        empty_a = {"id": "empty-a", "updated_at": "2026-06-01T00:00:00",
                   "pinned": False, "message_count": 0}
        empty_b = {"id": "empty-b", "updated_at": "2026-06-10T00:00:00",
                   "pinned": False, "message_count": 0}
        tie = [
            s["id"]
            for s in sorted(
                [empty_a, empty_b],
                key=lambda s: app_main._session_list_sort_key(
                    s, False, "updated_at"
                ),
                reverse=True,
            )
        ]
        tie_ok = tie == ["empty-b", "empty-a"]
        print(
            f"{PASS if tie_ok else FAIL} empty sessions tie-break by recency"
            f"{'' if tie_ok else f' — got {tie}'}"
        )
        return 0 if ok and tie_ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
