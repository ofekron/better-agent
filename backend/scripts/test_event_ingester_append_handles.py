"""Regression test for append-handle growth across many journal roots.

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_append_handles.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-handles-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import EventIngester  # noqa: E402
from event_ingester import _MAX_OPEN_APPEND_HANDLES  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _event(uid: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": uid}]},
    }


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _run() -> bool:
    ingester = EventIngester()

    for index in range(_MAX_OPEN_APPEND_HANDLES + 50):
        root_id = f"root-{index}"
        seq = ingester.ingest(
            root_id,
            sid=root_id,
            event_type="agent_message",
            data=_event(f"uid-{index}"),
            source="test",
            msg_id=f"msg-{index}",
        )
        if seq != 1:
            return _check(False, "each fresh root gets seq 1", f"{root_id=} {seq=}")

    ok = _check(
        len(ingester._handles) <= _MAX_OPEN_APPEND_HANDLES,
        "ingester bounds cached append handles after durable writes",
        f"open roots={list(ingester._handles)[:5]} count={len(ingester._handles)}",
    )

    hot_root = f"root-{_MAX_OPEN_APPEND_HANDLES + 49}"
    ok = _check(
        hot_root in ingester._handles,
        "latest hot root keeps append handle for next event",
        f"open roots={list(ingester._handles)[-5:]}",
    ) and ok

    seq = ingester.ingest(
        hot_root,
        sid=hot_root,
        event_type="agent_message",
        data=_event("uid-hot-root-second"),
        source="test",
        msg_id="msg-hot-root",
    )
    ok = _check(seq == 2, "warm cached handle preserves next seq", f"{seq=}") and ok
    ok = _check(
        hot_root in ingester._handles,
        "warm append handle remains cached after later write",
        f"open roots={list(ingester._handles)}",
    ) and ok

    ingester.close_all()
    ok = _check(
        not ingester._handles,
        "close_all releases cached append handles",
        f"open roots={list(ingester._handles)}",
    ) and ok
    return ok


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
