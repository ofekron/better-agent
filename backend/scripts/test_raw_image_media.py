from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-raw-image-media-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from file_browser import get_raw_file_info  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def test_raw_file_info_accepts_images() -> None:
    img = Path(_TMP_HOME) / "sample.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    info = get_raw_file_info(str(img))
    assert info["path"] == str(img.resolve())
    assert info["mime_type"] == "image/png"
    assert info["size"] == img.stat().st_size


TESTS = [
    ("raw file endpoint whitelist accepts png images", test_raw_file_info_accepts_images),
]


def main_run() -> int:
    failed = False
    passed = 0
    try:
        for name, fn in TESTS:
            try:
                fn()
            except AssertionError as exc:
                failed = True
                print(f"{FAIL}  {name}: {exc}")
                continue
            except Exception:
                failed = True
                import traceback
                traceback.print_exc()
                print(f"{FAIL}  {name} (error)")
                continue
            passed += 1
            print(f"{PASS}  {name}")
        print(f"{passed}/{len(TESTS)} subtests passed")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_run())
