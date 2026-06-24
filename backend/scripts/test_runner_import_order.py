import os
import shutil
import sys
import tempfile

import _test_home
TMP_HOME = _test_home.isolate("bc-test-runner-import-")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def main() -> int:
    try:
        import runner  # noqa: F401
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS runner imports before executing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
