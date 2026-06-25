#!/usr/bin/env python3
from pathlib import Path
import json
import logging
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner import (  # noqa: E402
    _fork_parent_line_count,
    _fork_prefix_byte_offset,
    _jsonl_byte_offset_after_lines,
)


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


def _line(uuid: str, text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def test_offset_after_parent_lines() -> bool:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fork.jsonl"
        prefix = _line("provision", "ready")
        current = _line("current", "work")
        path.write_text(prefix + current, encoding="utf-8")
        offset = _jsonl_byte_offset_after_lines(path, 1)
        if offset != len(prefix.encode("utf-8")):
            print(f"  expected offset {len(prefix.encode('utf-8'))}, got {offset}")
            return False
        remaining = path.read_bytes()[offset:]
        if b"ready" in remaining or b"current" not in remaining:
            print(f"  wrong remaining payload: {remaining!r}")
            return False
    return True


def test_unavailable_boundary_starts_at_eof() -> bool:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fork.jsonl"
        payload = _line("provision", "ready")
        path.write_text(payload, encoding="utf-8")
        offset = _fork_prefix_byte_offset(path, 2, logging.getLogger("test"))
        if offset != len(payload.encode("utf-8")):
            print(f"  expected EOF offset {len(payload.encode('utf-8'))}, got {offset}")
            return False
    return True


def test_fork_parent_line_count_rejects_non_dict_config() -> bool:
    if _fork_parent_line_count(["not", "a", "dict"]) != 0:
        print("  list config should return 0")
        return False
    if _fork_parent_line_count("fork_parent_line_count=9") != 0:
        print("  string config should return 0")
        return False
    if _fork_parent_line_count({"fork_parent_line_count": "3"}) != 3:
        print("  dict config should parse count")
        return False
    return True


TESTS = [
    ("offset after parent lines", test_offset_after_parent_lines),
    ("unavailable boundary starts at eof", test_unavailable_boundary_starts_at_eof),
    ("fork parent line count rejects non-dict config", test_fork_parent_line_count_rejects_non_dict_config),
]


def main() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
