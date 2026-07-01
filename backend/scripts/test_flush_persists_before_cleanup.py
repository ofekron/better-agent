#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import time
from unittest.mock import patch

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-flush-cleanup-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_manager import manager as session_manager  # noqa: E402


def main() -> int:
    sess = session_manager.create(
        name="cleanup", model="sonnet", cwd="/tmp/test-cleanup",
        orchestration_mode="native", source="cli",
    )
    session_manager.append_user_msg(sess["id"], {
        "id": "cleanup-user",
        "role": "user",
        "content": "flush before temp-home cleanup",
    })
    session_manager.flush_pending_persists()
    with patch("session_manager.logger.exception") as logger_exception:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        time.sleep(0.2)
    if logger_exception.call_count:
        print(f"FAIL late persist logged {logger_exception.call_count} exception(s)")
        return 1
    print("PASS flush_pending_persists prevents late tail write after temp-home cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
