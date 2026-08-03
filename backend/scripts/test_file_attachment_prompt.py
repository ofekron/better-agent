from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from file_attachment_prompt import prepend_file_attachments  # noqa: E402


def _file(name: str, raw: bytes, media_type: str = "text/plain") -> dict:
    return {
        "name": name,
        "media_type": media_type,
        "size": len(raw),
        "data": base64.b64encode(raw).decode("ascii"),
    }


def test_text_binary_and_filename_are_encoded_once():
    prompt = prepend_file_attachments("question", [
        _file('a\"<&.txt', b"hello"),
        _file("binary.bin", b"\xff", "application/octet-stream"),
    ])
    assert '<file name="a&quot;&lt;&amp;.txt">\nhello\n</file>' in prompt
    assert '<file name="binary.bin">[binary file, 1 bytes]</file>' in prompt
    assert prompt.endswith("\n\nquestion")


@pytest.mark.parametrize("field,value", [
    ("data", "not-base64"),
    ("size", 999),
])
def test_malformed_attachments_fail_closed(field, value):
    attachment = _file("a.txt", b"hello")
    attachment[field] = value
    with pytest.raises(ValueError):
        prepend_file_attachments("question", [attachment])
