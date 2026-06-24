#!/usr/bin/env python3
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-gemini-stderr-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner_gemini import _extract_stderr_error  # noqa: E402


def check(name: str, condition: bool) -> bool:
    if condition:
        print(f"PASS {name}")
        return True
    print(f"FAIL {name}")
    return False


def main() -> int:
    stderr = """YOLO mode is enabled. All tool calls will be automatically approved.
Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals.
    at throwIneligibleOrProjectIdError (file:///bundle.js:273244:11)
    at _doSetupUser (file:///bundle.js:273233:5)
    at process.processTicksAndRejections (node:internal/process/task_queues:104:5) {
  ineligibleTiers: [
    {
      reasonCode: 'UNSUPPORTED_CLIENT',
      reasonMessage: 'This client is no longer supported for Gemini Code Assist for individuals.',
    }
  ]
}
"""
    got = _extract_stderr_error(stderr)
    ok = [
        check("extracts named Gemini auth error", got is not None and "IneligibleTierError" in got),
        check("does not surface processTicks frame", got is not None and "processTicksAndRejections" not in got),
        check("falls back past stack frames", _extract_stderr_error("    at frame\nplain failure") == "plain failure"),
    ]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
