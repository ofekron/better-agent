from __future__ import annotations

from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES


def test_subprocess_line_limit_value() -> None:
    assert SUBPROCESS_LINE_LIMIT_BYTES == 32 * 1024 * 1024


def test_subprocess_line_limit_is_int() -> None:
    assert isinstance(SUBPROCESS_LINE_LIMIT_BYTES, int)
