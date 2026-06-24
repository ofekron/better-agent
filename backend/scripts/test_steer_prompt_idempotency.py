from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
TMP_HOME = _test_home.isolate("bc-test-steer-idempotent-")

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def check(name: str, value: bool, expected: bool) -> bool:
    ok = value is expected
    print(f"{PASS if ok else FAIL} {name}: got={value!r} expected={expected!r}")
    return ok


def main() -> int:
    try:
        from orchestrator import Coordinator

        msg = {
            "events": [{
                "type": "steer_prompt",
                "data": {
                    "client_id": "client-1",
                    "lifecycle_msg_id": "life-1",
                },
            }],
        }
        ok = True
        ok &= check(
            "dedupes same client_id",
            Coordinator._message_has_steer_prompt(
                msg, client_id="client-1", lifecycle_msg_id="life-x",
            ),
            True,
        )
        ok &= check(
            "dedupes same lifecycle_msg_id",
            Coordinator._message_has_steer_prompt(
                msg, client_id="client-x", lifecycle_msg_id="life-1",
            ),
            True,
        )
        ok &= check(
            "does not dedupe unrelated steer",
            Coordinator._message_has_steer_prompt(
                msg, client_id="client-x", lifecycle_msg_id="life-x",
            ),
            False,
        )
        return 0 if ok else 1
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
