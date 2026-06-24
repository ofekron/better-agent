from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-images-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402
from main import resolve_session_image_path  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_resolves_existing_session_image_inside_image_root() -> bool:
    session_id = "session-one"
    filename = "message-one_0.png"
    image_path = ba_home() / "sessions" / "images" / session_id / filename
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"png")
    return resolve_session_image_path(session_id, filename) == image_path.resolve()


def test_rejects_session_image_path_escape() -> bool:
    try:
        resolve_session_image_path("session-one", "../other.png")
    except HTTPException as exc:
        return exc.status_code == 404
    return False


def main() -> int:
    tests = [
        test_resolves_existing_session_image_inside_image_root,
        test_rejects_session_image_path_escape,
    ]
    failures = []
    try:
        for test in tests:
            ok = test()
            print(f"{PASS if ok else FAIL} {test.__name__}")
            if not ok:
                failures.append(test.__name__)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print(f"{FAIL} failures: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
