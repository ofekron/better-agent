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


def test_offset_after_parent_lines() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fork.jsonl"
        prefix = _line("provision", "ready")
        current = _line("current", "work")
        path.write_text(prefix + current, encoding="utf-8")
        offset = _jsonl_byte_offset_after_lines(path, 1)
        expected = len(prefix.encode("utf-8"))
        assert offset == expected, f"expected offset {expected}, got {offset}"
        remaining = path.read_bytes()[offset:]
        assert b"ready" not in remaining and b"current" in remaining, f"wrong remaining payload: {remaining!r}"


def test_unavailable_boundary_starts_at_eof() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fork.jsonl"
        payload = _line("provision", "ready")
        path.write_text(payload, encoding="utf-8")
        offset = _fork_prefix_byte_offset(path, 2, logging.getLogger("test"))
        expected = len(payload.encode("utf-8"))
        assert offset == expected, f"expected EOF offset {expected}, got {offset}"


def test_fork_parent_line_count_rejects_non_dict_config() -> None:
    assert _fork_parent_line_count(["not", "a", "dict"]) == 0, "list config should return 0"
    assert _fork_parent_line_count("fork_parent_line_count=9") == 0, "string config should return 0"
    assert _fork_parent_line_count({"fork_parent_line_count": "3"}) == 3, "dict config should parse count"


TESTS = [
    ("offset after parent lines", test_offset_after_parent_lines),
    ("unavailable boundary starts at eof", test_unavailable_boundary_starts_at_eof),
    ("fork parent line count rejects non-dict config", test_fork_parent_line_count_rejects_non_dict_config),
]


def main() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
            print(f"{FAIL}  {name}")
            failed += 1
        else:
            print(f"{PASS}  {name}")
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
