"""Regression: session list ordering must compare mixed ISO timestamps by time.

Run with:
    cd backend && .venv/bin/python scripts/test_session_list_timestamp_order.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-order-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _sessions_dir():
    d = ba_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record(sid: str, updated_at: str) -> dict:
    return {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "model": "gpt-5.5",
        "cwd": "/tmp/test-session-order",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [],
        "next_seq": 0,
        "created_at": "2026-06-16T00:00:00",
        "updated_at": updated_at,
        "source": "cli",
    }


def _write(record: dict) -> None:
    with open(_sessions_dir() / f"{record['id']}.json", "w") as f:
        json.dump(record, f)


def main() -> int:
    try:
        older = "old-local-naive"
        newer = "new-utc-offset"
        _write(_record(older, "2026-06-16T20:31:12.850407"))
        _write(_record(newer, "2026-06-16T17:32:25.765105+00:00"))

        order = [s["id"] for s in session_store.list_sessions()]
        ok = order[:2] == [newer, older]
        print(
            f"{PASS if ok else FAIL} mixed naive/local and UTC updated_at sort "
            f"chronologically{'' if ok else f' — got {order[:2]}'}"
        )

        import main as app_main  # noqa: E402

        selected_old = {
            "id": older,
            "updated_at": "2026-05-29T00:00:00",
            "pinned": False,
        }
        unselected_new = {
            "id": newer,
            "updated_at": "2026-06-16T00:00:00",
            "pinned": False,
        }
        pinned_old = {
            "id": "pinned-old",
            "updated_at": "2026-05-01T00:00:00",
            "pinned": True,
        }
        selected_order = [
            s["id"]
            for s in sorted(
                [selected_old, unselected_new],
                key=lambda s: app_main._session_list_sort_key(s, folder_view=True),
                reverse=True,
            )
        ]
        selected_ok = selected_order == [newer, older]
        print(
            f"{PASS if selected_ok else FAIL} selected session does not override "
            f"updated_at sort{'' if selected_ok else f' — got {selected_order}'}"
        )
        pinned_order = [
            s["id"]
            for s in sorted(
                [selected_old, unselected_new, pinned_old],
                key=lambda s: app_main._session_list_sort_key(s, folder_view=True),
                reverse=True,
            )
        ]
        pinned_ok = pinned_order[0] == "pinned-old"
        print(
            f"{PASS if pinned_ok else FAIL} pinned sessions still outrank "
            f"recency{'' if pinned_ok else f' — got {pinned_order}'}"
        )
        return 0 if ok and selected_ok and pinned_ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
